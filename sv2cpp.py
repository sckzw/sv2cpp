#!/usr/bin/env python3
"""
sv2cpp.py - Generic SystemVerilog to Bare-metal / Embedded C++ Transpiler using pyslang

Features:
- Generic AST-level analysis and transpilation from SystemVerilog to modern C++ (C++14/17/20)
- Natural C++ reference semantics (Type&) for output/inout/ref subroutine arguments
- std::string support for SystemVerilog string types, string concatenations, $sformat, and $sformatf
- std::vector support for SystemVerilog queues ([$]) and dynamic arrays with built-in methods
  (push_back, pop_front, pop_back, size, delete/clear, insert, sort, rsort, reverse)
- C++ struct definitions and designated / uniform list initializations (Type{...})
- Call-graph reachability analysis with boundary task mapping to prune simulation harnesses
- AST-level symbolic tracking of dynamic file paths ($fopen, $sformat, $sformatf, string concat)
- Automatic generation of bare-metal ROMFS asset tables (MemAssetEntry) with multi-mode embedding
- Seamless interop with C HAL APIs (extern "C") and bare-metal runtime (mem_file, plusargs)
"""

import sys
import re
import shlex
import argparse
import itertools
from pathlib import Path

# Ensure pyslang_env is in sys.path if running locally
env_path = Path(__file__).resolve().parent / "pyslang_env"
if env_path.exists() and str(env_path) not in sys.path:
    sys.path.insert(0, str(env_path))

import pyslang
import yaml


class ASTPathExpander:
    """
    Statically analyzes AST to discover dynamically formatted file paths ($fopen, $sformat, etc.)
    by abstract interpretation / symbolic evaluation of loop bounds, variables, ternary operators,
    and string concatenations without hardcoded module assumptions.
    Supports interprocedural call-graph traversal with automatic effect analysis and symbolic argument binding.
    """
    FILE_SYS_CALLS = {
        "$fopen", "$fread", "$fgets", "$readmemh", "$readmemb",
        "$fwrite", "$fdisplay", "$fclose", "$fscanf", "$sformat", "$sformatf"
    }

    def __init__(self, workdir=".", all_subroutines=None):
        self.workdir = workdir
        self.all_subroutines = all_subroutines or {}
        self.input_paths = set()
        self.output_paths = set()
        self.var_values = {}
        self.unresolved_dynamic_paths = set()
        self.visited_calls = set()
        self.call_depth = 0
        self.max_call_depth = 12
        self.effectful_subs = self._analyze_subroutine_effects()

    def _analyze_subroutine_effects(self):
        relevant = set()
        for name, sub_info in self.all_subroutines.items():
            sub_sym = sub_info["symbol"] if isinstance(sub_info, dict) else sub_info
            if not hasattr(sub_sym, "body"):
                continue
            has_effect = False
            def check(n):
                nonlocal has_effect
                if type(n).__name__ == "CallExpression":
                    s = getattr(n, "subroutine", None)
                    if hasattr(s, "subroutine") and getattr(s.subroutine, "name", None) in self.FILE_SYS_CALLS:
                        has_effect = True
                return pyslang.ast.VisitAction.Advance
            sub_sym.body.visit(check)
            if has_effect:
                relevant.add(name)

        changed = True
        while changed:
            changed = False
            for name, sub_info in self.all_subroutines.items():
                if name in relevant:
                    continue
                sub_sym = sub_info["symbol"] if isinstance(sub_info, dict) else sub_info
                if not hasattr(sub_sym, "body"):
                    continue
                calls_relevant = False
                def check_transitive(n):
                    nonlocal calls_relevant
                    if type(n).__name__ == "CallExpression":
                        s = getattr(n, "subroutine", None)
                        if hasattr(s, "name") and s.name in relevant:
                            calls_relevant = True
                    return pyslang.ast.VisitAction.Advance
                sub_sym.body.visit(check_transitive)
                if calls_relevant:
                    relevant.add(name)
                    changed = True

        return relevant

    def get_var_name(self, expr):
        if expr is None:
            return None
        if not hasattr(expr, "kind") or isinstance(expr.kind, pyslang.ast.SymbolKind):
            return getattr(expr, "name", None)
        k = expr.kind
        if k == pyslang.ast.ExpressionKind.Assignment:
            return self.get_var_name(expr.left)
        if k == pyslang.ast.ExpressionKind.NamedValue:
            return expr.symbol.name
        if k == pyslang.ast.ExpressionKind.MemberAccess:
            base = self.get_var_name(expr.value)
            m_name = expr.member.name
            return f"{base}.{m_name}" if base else m_name
        if k == pyslang.ast.ExpressionKind.ElementSelect:
            base = self.get_var_name(expr.value)
            idx_vals = self.eval_expr(expr.selector)
            if len(idx_vals) == 1:
                return f"{base}[{list(idx_vals)[0]}]"
            return f"{base}[]"
        if k == pyslang.ast.ExpressionKind.Conversion:
            return self.get_var_name(expr.operand)
        if hasattr(expr, "symbol"):
            return expr.symbol.name
        return None

    def eval_expr(self, expr):
        if expr is None:
            return set()
        k = expr.kind
        if k == pyslang.ast.ExpressionKind.Conversion:
            return self.eval_expr(expr.operand)
        elif k == pyslang.ast.ExpressionKind.IntegerLiteral:
            return {int(expr.value)}
        elif k == pyslang.ast.ExpressionKind.StringLiteral:
            return {str(expr.value)}
        elif k == pyslang.ast.ExpressionKind.NamedValue:
            v_name = expr.symbol.name
            return self.var_values.get(v_name, set())
        elif k == pyslang.ast.ExpressionKind.MemberAccess:
            base = self.get_var_name(expr.value)
            m_name = expr.member.name
            val = self.var_values.get(f"{base}.{m_name}")
            if not val and base and "[" in base:
                base_arr = base.split("[")[0]
                val = self.var_values.get(f"{base_arr}[].{m_name}")
            if not val and base:
                base_arr = base.split("[")[0]
                val = self.var_values.get(f"{base_arr}.{m_name}")
            return val or set()
        elif k == pyslang.ast.ExpressionKind.ElementSelect:
            base = self.get_var_name(expr.value)
            return self.var_values.get(base, set())
        elif k == pyslang.ast.ExpressionKind.BinaryOp:
            left_s = self.eval_expr(expr.left)
            right_s = self.eval_expr(expr.right)
            res = set()
            for l in left_s:
                for r in right_s:
                    if isinstance(l, int) and isinstance(r, int):
                        if expr.op == pyslang.ast.BinaryOperator.Add: res.add(l + r)
                        elif expr.op == pyslang.ast.BinaryOperator.Subtract: res.add(l - r)
                        elif expr.op == pyslang.ast.BinaryOperator.Multiply: res.add(l * r)
                        elif expr.op == pyslang.ast.BinaryOperator.Divide and r != 0: res.add(l // r)
                        elif expr.op == pyslang.ast.BinaryOperator.LogicalShiftLeft: res.add(l << r)
                        elif expr.op == pyslang.ast.BinaryOperator.LogicalShiftRight: res.add(l >> r)
            return res
        elif k == pyslang.ast.ExpressionKind.ConditionalOp:
            return self.eval_expr(expr.left) | self.eval_expr(expr.right)
        elif k == pyslang.ast.ExpressionKind.Concatenation:
            sub_sets = []
            has_unknown = False
            for op in expr.operands:
                s = self.eval_expr(op)
                if not s:
                    v_name = self.get_var_name(op) or str(op.syntax).strip()
                    s = {f"<{v_name}>"}
                    has_unknown = True
                sub_sets.append(s)
            res = set()
            for prod in itertools.product(*sub_sets):
                res.add("".join(str(p) for p in prod))
            if has_unknown:
                for r in res:
                    if "/" in r or "." in r:
                        self.unresolved_dynamic_paths.add(r)
                return set()
            return res
        elif k == pyslang.ast.ExpressionKind.Call:
            sub = getattr(getattr(expr, "subroutine", None), "subroutine", None)
            if sub and sub.name == "$sformatf":
                return self.eval_sformatf(expr)
            elif sub and sub.name in ("$urandom_range", "urandom_range"):
                args = [self.eval_expr(a) for a in expr.arguments]
                if len(args) >= 1 and args[0]:
                    max_v = max(args[0])
                    min_v = min(args[1]) if len(args) >= 2 and args[1] else 0
                    if min_v > max_v:
                        min_v, max_v = max_v, min_v
                    if max_v - min_v <= 64:
                        return set(range(min_v, max_v + 1))
                    return {min_v, max_v}
        return set()

    def eval_sformatf(self, call_expr):
        fmt_expr = getattr(call_expr.arguments[0], "operand", call_expr.arguments[0])
        fmt = getattr(fmt_expr, "value", str(fmt_expr.syntax).strip(' "'))
        fmt_c = _normalize_sv_format(fmt)

        arg_sets = []
        has_unknown = False
        for a in call_expr.arguments[1:]:
            s = self.eval_expr(a)
            if not s:
                v_name = self.get_var_name(a) or str(a.syntax).strip()
                s = {f"<{v_name}>"}
                has_unknown = True
            arg_sets.append(s)

        res = set()
        for prod in itertools.product(*arg_sets):
            try:
                res.add(fmt_c % prod)
            except Exception:
                try:
                    fmt_generic = re.sub(r"%[0-9]*[dxXhHoObBu]", "%s", fmt_c)
                    res.add(fmt_generic % prod)
                except Exception:
                    res.add(f"{fmt_c}_{prod}")

        if has_unknown:
            for r in res:
                if "/" in r or "." in r:
                    self.unresolved_dynamic_paths.add(r)
            return set()
        return res

    def handle_assignment(self, lval, rval):
        l_name = self.get_var_name(lval)
        if not l_name:
            return
        vals = self.eval_expr(rval)
        if vals:
            self.var_values[l_name] = vals
            if "[" in l_name:
                base_arr = l_name.split("[")[0]
                if "." in l_name:
                    field = l_name.split(".", 1)[1]
                    self.var_values.setdefault(f"{base_arr}[].{field}", set()).update(vals)
                else:
                    self.var_values.setdefault(f"{base_arr}[]", set()).update(vals)

    def walk_statement(self, stmt):
        if stmt is None:
            return
        k = stmt.kind
        if k == pyslang.ast.StatementKind.List:
            for s in stmt.list:
                self.walk_statement(s)
        elif k == pyslang.ast.StatementKind.Block:
            self.walk_statement(stmt.body)
        elif k == pyslang.ast.StatementKind.VariableDeclaration:
            var_sym = stmt.symbol
            if getattr(var_sym, "initializer", None):
                self.handle_assignment(var_sym, var_sym.initializer)
                self.check_calls(var_sym.initializer)
        elif k == pyslang.ast.StatementKind.ExpressionStatement:
            self.check_calls(stmt.expr)
        elif k == pyslang.ast.StatementKind.Conditional:
            for cond in getattr(stmt, "conditions", []):
                if hasattr(cond, "expr") and cond.expr:
                    self.check_calls(cond.expr)
            self.walk_statement(stmt.ifTrue)
            if hasattr(stmt, "ifFalse") and stmt.ifFalse:
                self.walk_statement(stmt.ifFalse)
        elif k == pyslang.ast.StatementKind.Case:
            self.check_calls(getattr(stmt, "expr", None))
            for item in stmt.items:
                self.walk_statement(item.stmt)
            if stmt.defaultCase:
                self.walk_statement(stmt.defaultCase)
        elif k == pyslang.ast.StatementKind.ForLoop:
            self.handle_for(stmt)
        elif k in (pyslang.ast.StatementKind.WhileLoop, pyslang.ast.StatementKind.RepeatLoop, pyslang.ast.StatementKind.DoWhileLoop):
            if hasattr(stmt, "cond") and stmt.cond:
                self.check_calls(stmt.cond)
            self.walk_statement(stmt.body)
        elif k == pyslang.ast.StatementKind.Return:
            if hasattr(stmt, "expr") and stmt.expr:
                self.check_calls(stmt.expr)
        elif k == pyslang.ast.StatementKind.Timed:
            if hasattr(stmt, "stmt") and stmt.stmt:
                self.walk_statement(stmt.stmt)
        elif k == pyslang.ast.StatementKind.Wait:
            if hasattr(stmt, "stmt") and stmt.stmt:
                self.walk_statement(stmt.stmt)

    def check_calls(self, expr):
        if expr is None:
            return
        k = expr.kind
        if k == pyslang.ast.ExpressionKind.Assignment:
            self.handle_assignment(expr.left, expr.right)
            self.check_calls(expr.right)
        elif k == pyslang.ast.ExpressionKind.Conversion:
            self.check_calls(getattr(expr, "operand", None))
        elif k == pyslang.ast.ExpressionKind.UnaryOp:
            self.check_calls(getattr(expr, "operand", None))
        elif k == pyslang.ast.ExpressionKind.BinaryOp:
            self.check_calls(getattr(expr, "left", None))
            self.check_calls(getattr(expr, "right", None))
        elif k == pyslang.ast.ExpressionKind.ConditionalOp:
            for cond in getattr(expr, "conditions", []):
                if hasattr(cond, "expr") and cond.expr:
                    self.check_calls(cond.expr)
            self.check_calls(getattr(expr, "left", None))
            self.check_calls(getattr(expr, "right", None))
        elif k == pyslang.ast.ExpressionKind.Call:
            sub = getattr(expr, "subroutine", None)
            if hasattr(sub, "subroutine"):
                sys_name = sub.subroutine.name
                if sys_name == "$sformat":
                    target = expr.arguments[0]
                    fmt_s = expr.arguments[1]
                    pseudo_call = type("PseudoCall", (), {
                        "arguments": [fmt_s] + list(expr.arguments[2:])
                    })
                    paths = self.eval_sformatf(pseudo_call)
                    t_name = self.get_var_name(target)
                    if t_name and paths:
                        self.var_values[t_name] = paths
                elif sys_name in ("$fopen", "$readmemh", "$readmemb", "$fread"):
                    path_expr = expr.arguments[0]
                    mode = "r"
                    if sys_name == "$fopen" and len(expr.arguments) > 1:
                        mode_s = self.eval_expr(expr.arguments[1])
                        if mode_s:
                            mode = list(mode_s)[0]
                    paths = self.eval_expr(path_expr)
                    if not paths:
                        v_name = self.get_var_name(path_expr) or str(path_expr.syntax).strip()
                        if "/" in v_name or "." in v_name:
                            self.unresolved_dynamic_paths.add(v_name)
                    else:
                        for p in paths:
                            if "w" in mode or "a" in mode:
                                self.output_paths.add(p)
                            else:
                                self.add_input_path(p)
            elif hasattr(sub, "name"):
                self.call_subroutine(sub.name, expr.arguments)

    def call_subroutine(self, sub_name, arguments):
        if self.call_depth >= self.max_call_depth:
            return
        if sub_name not in self.all_subroutines:
            return
        if sub_name not in self.effectful_subs:
            return

        sub_info = self.all_subroutines[sub_name]
        sub_sym = sub_info["symbol"] if isinstance(sub_info, dict) else sub_info
        if not hasattr(sub_sym, "body"):
            return

        call_sig = (sub_name, tuple(self.get_var_name(a) or str(a.syntax).strip() for a in arguments))
        if call_sig in self.visited_calls:
            return
        self.visited_calls.add(call_sig)

        saved_var_values = dict(self.var_values)
        self.call_depth += 1
        try:
            for param, arg in zip(sub_sym.arguments, arguments):
                p_name = param.name
                vals = self.eval_expr(arg)
                if vals:
                    self.var_values[p_name] = vals
                else:
                    arg_name = self.get_var_name(arg)
                    if arg_name and arg_name in self.var_values:
                        self.var_values[p_name] = self.var_values[arg_name]

            self.walk_statement(sub_sym.body)

            for param, arg in zip(sub_sym.arguments, arguments):
                if str(param.direction) in ("ArgumentDirection.Out", "ArgumentDirection.InOut"):
                    p_name = param.name
                    arg_name = self.get_var_name(arg)
                    if p_name in self.var_values and arg_name:
                        saved_var_values[arg_name] = self.var_values[p_name]
        finally:
            self.var_values = saved_var_values
            self.call_depth -= 1

    def handle_for(self, for_stmt):
        inits = getattr(for_stmt, "initializers", [])
        loop_var = None
        start_val = 0
        for init in inits:
            if init.kind == pyslang.ast.ExpressionKind.Assignment:
                loop_var = self.get_var_name(init.left)
                s_vals = self.eval_expr(init.right)
                if s_vals:
                    start_val = min(s_vals)

        stop_val = None
        if hasattr(for_stmt, "stopExpr") and for_stmt.stopExpr:
            stop_expr = for_stmt.stopExpr
            if stop_expr.kind == pyslang.ast.ExpressionKind.BinaryOp:
                lv = self.get_var_name(stop_expr.left)
                rv = self.get_var_name(stop_expr.right)
                if loop_var is None:
                    if lv and lv in self.var_values:
                        loop_var = lv
                        s_vals = self.var_values[lv]
                        if s_vals:
                            start_val = min(s_vals)
                    elif rv and rv in self.var_values:
                        loop_var = rv
                        s_vals = self.var_values[rv]
                        if s_vals:
                            start_val = min(s_vals)
                if lv == loop_var:
                    r_vals = self.eval_expr(stop_expr.right)
                    if r_vals:
                        max_r = max(r_vals)
                        if getattr(stop_expr, "op", None) == pyslang.ast.BinaryOperator.LessThanEqual:
                            stop_val = max_r + 1
                        else:
                            stop_val = max_r
                elif rv == loop_var:
                    l_vals = self.eval_expr(stop_expr.left)
                    if l_vals:
                        max_l = max(l_vals)
                        if getattr(stop_expr, "op", None) == pyslang.ast.BinaryOperator.GreaterThanEqual:
                            stop_val = max_l + 1
                        else:
                            stop_val = max_l

        if loop_var and isinstance(start_val, int) and isinstance(stop_val, int) and abs(stop_val - start_val) <= 128:
            step = 1 if stop_val >= start_val else -1
            saved_val = self.var_values.get(loop_var)
            for v in range(start_val, stop_val, step):
                self.var_values[loop_var] = {v}
                self.walk_statement(for_stmt.body)
            if saved_val is not None:
                self.var_values[loop_var] = saved_val
            else:
                self.var_values.pop(loop_var, None)
        else:
            self.walk_statement(for_stmt.body)

    def add_input_path(self, p):
        if "<" in p or ">" in p:
            self.unresolved_dynamic_paths.add(p)
            return
        self.input_paths.add(p)


def _normalize_sv_format(fmt: str) -> str:
    """Convert SystemVerilog format specifiers (%0d, %0h, %h, %0t, etc.) to C printf/scanf equivalents."""
    res = fmt.replace("%0d", "%d").replace("%0h", "%x").replace("%0H", "%X").replace("%0t", "%d").replace("%t", "%d")
    res = re.sub(r"%(\d*)h", r"%\1x", res)
    res = re.sub(r"%(\d*)H", r"%\1X", res)
    return res


def sv_type_to_cpp(sv_type):
    """Map SystemVerilog type symbol to (cpp_base_type, array_dim_str)."""
    if sv_type is None:
        return "uint32_t", ""

    is_dynamic = getattr(sv_type, "isQueue", False) or getattr(sv_type, "isDynamicArray", False)
    if str(getattr(sv_type, "kind", "")) == "SymbolKind.DynamicArrayType":
        is_dynamic = True

    if is_dynamic:
        inner = getattr(sv_type, "elementType", None)
        dims = []
        curr = inner
        while curr and (getattr(curr, "isUnpackedArray", False) or str(getattr(curr, "kind", "")) == "SymbolKind.FixedSizeUnpackedArrayType"):
            w = curr.fixedRange.width if hasattr(curr, "fixedRange") else 0
            dims.append(w)
            curr = getattr(curr, "elementType", None)
        base_t, _ = sv_type_to_cpp(curr)
        if not dims:
            return f"std::vector<{base_t}>", ""
        elif len(dims) == 1:
            return f"std::vector<Array1D<{base_t}, {dims[0]}>>", ""
        elif len(dims) == 2:
            return f"std::vector<Array2D<{base_t}, {dims[0]}, {dims[1]}>>", ""
        elif len(dims) == 3:
            return f"std::vector<Array3D<{base_t}, {dims[0]}, {dims[1]}, {dims[2]}>>", ""
        return f"std::vector<{base_t}>", ""

    if getattr(sv_type, "isUnpackedArray", False) or str(getattr(sv_type, "kind", "")) == "SymbolKind.FixedSizeUnpackedArrayType":
        dims = []
        curr = sv_type
        while curr and (getattr(curr, "isUnpackedArray", False) or str(getattr(curr, "kind", "")) == "SymbolKind.FixedSizeUnpackedArrayType"):
            w = curr.fixedRange.width if hasattr(curr, "fixedRange") else 0
            dims.append(w)
            curr = getattr(curr, "elementType", None)
        base_t, _ = sv_type_to_cpp(curr)
        dim_str = "".join(f"[{d}]" for d in dims)
        return base_t, dim_str

    if getattr(sv_type, "isString", False):
        return "std::string", ""

    if getattr(sv_type, "isFloating", False):
        w = getattr(sv_type, "bitWidth", 64)
        return ("float" if w <= 32 else "double"), ""

    if getattr(sv_type, "isStruct", False) or getattr(sv_type, "isAlias", False):
        t_name = getattr(sv_type, "name", "")
        if t_name and t_name != "$unit":
            return t_name, ""

    if getattr(sv_type, "isIntegral", False):
        w = sv_type.bitWidth
        if sv_type.isSigned:
            if w <= 8: return "int8_t", ""
            if w <= 16: return "int16_t", ""
            if w <= 32: return "int32_t", ""
            return "int64_t", ""
        else:
            if w == 1: return "bool", ""
            if w <= 8: return "uint8_t", ""
            if w <= 16: return "uint16_t", ""
            if w <= 32: return "uint32_t", ""
            return "uint64_t", ""

    t_name = getattr(sv_type, "name", "")
    if t_name and t_name != "$unit":
        return t_name, ""
    return "uint32_t", ""


class CppExprTranspiler:
    """Transpiles SystemVerilog expressions (AST) into clean modern C++ expressions."""

    def __init__(self, task_mappings=None, var_mappings=None, out_params=None, source_manager=None, discovered_macros=None, cpp_std="c++17"):
        self.task_mappings = task_mappings or {}
        self.var_mappings = var_mappings or {}
        self.out_params = out_params or set()
        self.source_manager = source_manager
        self.discovered_macros = discovered_macros if discovered_macros is not None else {}
        self.cpp_std = cpp_std
        self.current_lval = None
        self.has_unsupported = False
        self.unsupported_reason = ""
        self._skip_macro_lookup = False

    def _is_parenthesized(self, expr):
        """Check if the expression was explicitly enclosed in parentheses in the original SystemVerilog source."""
        curr = expr
        while curr:
            if hasattr(curr, "syntax") and curr.syntax:
                if getattr(curr.syntax, "kind", None) == pyslang.syntax.SyntaxKind.ParenthesizedExpression:
                    return True
            if getattr(curr, "kind", None) == pyslang.ast.ExpressionKind.Conversion:
                curr = getattr(curr, "operand", None)
            else:
                break
        return False

    def get_macro_expansion_name(self, expr):
        if not self.source_manager or not hasattr(expr, "sourceRange") or not expr.sourceRange:
            return None
        sr = expr.sourceRange
        start, end = sr.start, sr.end
        if not start or not end:
            return None
        if not self.source_manager.isMacroLoc(start) or self.source_manager.isMacroArgLoc(start):
            return None
        if start.buffer != end.buffer:
            return None
        name = self.source_manager.getMacroName(start)
        if name and re.match(r"^[a-zA-Z_]\w*$", name):
            return name
        return None

    def get_cpp_type_name(self, sv_type):
        """Extract valid C++ type name from SystemVerilog type symbol."""
        if sv_type is None:
            return ""
        base_t, arr_dim = sv_type_to_cpp(sv_type)
        if arr_dim:
            return ""
        return base_t

    def eval_int_val(self, e):
        if hasattr(e, "constant") and e.constant:
            try:
                return int(e.constant.convertToInt().value)
            except Exception:
                pass
        if hasattr(e, "value"):
            try:
                return int(e.value)
            except Exception:
                pass
        try:
            return int(str(e.syntax).strip())
        except Exception:
            return 0

    def _is_integral_select(self, expr):
        if expr is None or not hasattr(expr, "value") or expr.value is None:
            return False
        vt = expr.value.type
        is_unpacked = getattr(vt, "isUnpackedArray", False)
        is_queue = getattr(vt, "isQueue", False) or getattr(vt, "isDynamicArray", False)
        is_string = getattr(vt, "isString", False)
        return not is_unpacked and not is_queue and not is_string and getattr(vt, "isIntegral", False)

    def _get_range_lo_and_mask(self, expr):
        sel_kind = getattr(expr, "selectionKind", None)
        if sel_kind == pyslang.ast.RangeSelectionKind.IndexedUp:
            width = self.eval_int_val(expr.right)
            lo_str = f"({self.transpile(expr.left)})"
            lo_is_zero = False
        elif sel_kind == pyslang.ast.RangeSelectionKind.IndexedDown:
            width = self.eval_int_val(expr.right)
            lo_str = f"(({self.transpile(expr.left)}) - {width - 1})"
            lo_is_zero = False
        else:
            left_idx = self.eval_int_val(expr.left)
            right_idx = self.eval_int_val(expr.right)
            hi = max(left_idx, right_idx)
            lo = min(left_idx, right_idx)
            width = hi - lo + 1
            lo_str = str(lo)
            lo_is_zero = (lo == 0)
        mask = (1 << width) - 1 if 0 < width < 64 else ((1 << 64) - 1)
        return lo_str, lo_is_zero, mask

    def _transpile_concat_operands(self, operands):
        is_str_concat = any(
            getattr(op.type, "isString", False) or op.kind == pyslang.ast.ExpressionKind.StringLiteral
            for op in operands
        )
        if is_str_concat:
            parts = []
            for i, op in enumerate(operands):
                op_str = self.transpile(op)
                if i == 0:
                    parts.append(f"std::string({op_str})")
                else:
                    parts.append(op_str)
            return f"({' + '.join(parts)})"

        curr_shift = 0
        parts = []
        for op in reversed(operands):
            w = op.type.bitWidth if hasattr(op.type, "bitWidth") and op.type.bitWidth > 0 else 8
            op_c = self.transpile(op)
            if getattr(op.type, "isSigned", False) and 0 < w < 64:
                mask = (1 << w) - 1
                val_u64 = f"(static_cast<uint64_t>({op_c}) & 0x{mask:x}ULL)"
            else:
                val_u64 = f"static_cast<uint64_t>({op_c})"
            if curr_shift == 0:
                parts.append(val_u64)
            else:
                parts.append(f"({val_u64} << {curr_shift})")
            curr_shift += w
        parts.reverse()
        return f"({' | '.join(parts)})"

    def _format_varargs(self, fmt_arg, rest_args):
        fmt = self.transpile(fmt_arg)
        if getattr(fmt_arg.type, "isString", False) and fmt_arg.kind != pyslang.ast.ExpressionKind.StringLiteral:
            c_fmt = f"{fmt}.c_str()"
        else:
            c_fmt = _normalize_sv_format(fmt)
        args = []
        for a in rest_args:
            a_c = self.transpile(a)
            if getattr(a.type, "isString", False) and a.kind != pyslang.ast.ExpressionKind.StringLiteral:
                args.append(f"{a_c}.c_str()")
            else:
                args.append(a_c)
        other_args = (", " + ", ".join(args)) if args else ""
        return c_fmt, other_args

    def transpile(self, expr):
        if expr is None:
            return ""

        # Macro preservation & AST value extraction
        if not self._skip_macro_lookup and self.source_manager:
            macro_name = self.get_macro_expansion_name(expr)
            if macro_name:
                if macro_name not in self.discovered_macros:
                    inner_expr = expr
                    while inner_expr and inner_expr.kind == pyslang.ast.ExpressionKind.Conversion:
                        inner_expr = getattr(inner_expr, "operand", None)
                    if inner_expr is None:
                        inner_expr = expr

                    saved_sm = self.source_manager
                    self.source_manager = None
                    self._skip_macro_lookup = True
                    try:
                        raw_val = self.transpile(inner_expr)
                        self.discovered_macros[macro_name] = raw_val
                    finally:
                        self.source_manager = saved_sm
                        self._skip_macro_lookup = False
                return macro_name

        k = expr.kind

        # Assignment expression (e.g. for-loop inits/steps or inline assignments)
        if k == pyslang.ast.ExpressionKind.Assignment:
            has_rhs = (
                hasattr(expr, "right")
                and expr.right is not None
                and expr.right.kind != pyslang.ast.ExpressionKind.EmptyArgument
            )
            if has_rhs and expr.left.kind == pyslang.ast.ExpressionKind.ElementSelect and self._is_integral_select(expr.left):
                base_c = self.transpile(expr.left.value)
                idx_c = self.transpile(expr.left.selector)
                prev_lval = self.current_lval
                self.current_lval = self.transpile(expr.left)
                try:
                    right_c = self.transpile(expr.right)
                finally:
                    self.current_lval = prev_lval
                return f"{base_c} = ({base_c} & ~(1ULL << ({idx_c}))) | ((static_cast<uint64_t>({right_c}) & 1ULL) << ({idx_c}))"

            if has_rhs and expr.left.kind == pyslang.ast.ExpressionKind.RangeSelect and self._is_integral_select(expr.left):
                base_c = self.transpile(expr.left.value)
                lo_str, lo_is_zero, mask = self._get_range_lo_and_mask(expr.left)
                prev_lval = self.current_lval
                self.current_lval = self.transpile(expr.left)
                try:
                    right_c = self.transpile(expr.right)
                finally:
                    self.current_lval = prev_lval
                if lo_is_zero:
                    return f"{base_c} = ({base_c} & ~0x{mask:x}ULL) | (static_cast<uint64_t>({right_c}) & 0x{mask:x}ULL)"
                return f"{base_c} = ({base_c} & ~(0x{mask:x}ULL << {lo_str})) | ((static_cast<uint64_t>({right_c}) & 0x{mask:x}ULL) << {lo_str})"

            left_c = self.transpile(expr.left)
            if has_rhs:
                prev_lval = self.current_lval
                self.current_lval = left_c
                try:
                    right_c = self.transpile(expr.right)
                finally:
                    self.current_lval = prev_lval
                return f"{left_c} = {right_c}"
            return left_c

        # Empty argument (omitted optional argument in SV)
        if k == pyslang.ast.ExpressionKind.EmptyArgument:
            return ""

        # Type conversion / cast
        if k == pyslang.ast.ExpressionKind.Conversion:
            op_c = self.transpile(expr.operand)
            # If operand is string, return op_c directly or wrap in std::string if operand is literal
            if expr.operand.kind == pyslang.ast.ExpressionKind.StringLiteral:
                return op_c
            if getattr(expr.type, "isString", False):
                return f"std::string({op_c})"
            if getattr(expr.operand.type, "isString", False):
                return op_c
            cpp_type = self.get_cpp_type_name(expr.type)
            if cpp_type in ["uint64_t", "int64_t", "uint32_t", "int32_t", "uint16_t", "int16_t", "uint8_t", "int8_t", "bool"]:
                return f"static_cast<{cpp_type}>({op_c})"
            return op_c

        # Literal integers with radix preservation
        elif k == pyslang.ast.ExpressionKind.IntegerLiteral:
            val = int(expr.value)
            syntax_str = str(expr.syntax).strip()
            if "'h" in syntax_str or "'H" in syntax_str or "0x" in syntax_str:
                m_hex = re.search(r"\'[hH]([0-9a-fA-F_]+)", syntax_str)
                if m_hex:
                    return f"0x{m_hex.group(1).replace('_', '')}"
                return f"0x{val:x}"
            return str(val)

        # Real Literal
        elif k == pyslang.ast.ExpressionKind.RealLiteral:
            return str(float(expr.value))

        # Time Literal
        elif k == pyslang.ast.ExpressionKind.TimeLiteral:
            return str(expr.value)

        # Null Literal
        elif k == pyslang.ast.ExpressionKind.NullLiteral:
            return "nullptr"

        # UnbasedUnsizedIntegerLiteral ('0, '1, etc.)
        elif k == pyslang.ast.ExpressionKind.UnbasedUnsizedIntegerLiteral:
            val_char = str(expr.value).lower()
            if val_char == '1':
                return "~0ULL"
            return "0"

        # String literal
        elif k == pyslang.ast.ExpressionKind.StringLiteral:
            val_str = str(expr.value)
            escaped = val_str.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return f'"{escaped}"'

        # Named value (variable, parameter, enum)
        elif k == pyslang.ast.ExpressionKind.NamedValue:
            name = expr.symbol.name
            if name in self.var_mappings:
                return self.var_mappings[name]
            # In C++, out_params are pass-by-reference (Type&), so no (*name) dereference is required!
            return name

        # Member access: struct.field
        elif k == pyslang.ast.ExpressionKind.MemberAccess:
            base_str = self.transpile(expr.value)
            return f"{base_str}.{expr.member.name}"

        # Simple assignment pattern: '{"gradient", 32, 24, 0, 8} or '{0,0,...}
        # In C++, transpiles cleanly to uniform list initialization: vector_t{ "gradient", 32, 24, 0, 8 }
        elif k == pyslang.ast.ExpressionKind.SimpleAssignmentPattern:
            cpp_type = self.get_cpp_type_name(expr.type)
            elem_strs = [self.transpile(e) for e in expr.elements]
            prefix = f"{cpp_type}" if cpp_type else ""
            return f"{prefix}{{ {', '.join(elem_strs)} }}"

        # Structured assignment pattern: '{member: val, ...} or '{default: 0}
        elif k == pyslang.ast.ExpressionKind.StructuredAssignmentPattern:
            cpp_type = self.get_cpp_type_name(expr.type)
            prefix = f"{cpp_type}" if cpp_type else ""
            parts = []
            if (self.cpp_std == "c++20" and hasattr(expr, "memberSetters") and expr.memberSetters and
                    not getattr(expr, "defaultSetter", None)):
                for ms in expr.memberSetters:
                    m_name = ms.member.name
                    m_val = self.transpile(ms.expr)
                    parts.append(f".{m_name} = {m_val}")
            elif hasattr(expr, "elements") and expr.elements:
                for el in expr.elements:
                    parts.append(self.transpile(el))
            elif hasattr(expr, "defaultSetter") and expr.defaultSetter:
                parts.append(self.transpile(expr.defaultSetter))
            return f"{prefix}{{ {', '.join(parts)} }}"

        # Replicated assignment pattern: '{N{val}}
        elif k == pyslang.ast.ExpressionKind.ReplicatedAssignmentPattern:
            cpp_type = self.get_cpp_type_name(expr.type)
            prefix = f"{cpp_type}" if cpp_type else ""
            count = self.eval_int_val(expr.count) if hasattr(expr, "count") else 1
            sub_elements = [self.transpile(e) for e in expr.elements] if hasattr(expr, "elements") else []
            all_elements = sub_elements * count
            return f"{prefix}{{ {', '.join(all_elements)} }}"

        # LValue reference (for assignments like samples += ...)
        elif k == pyslang.ast.ExpressionKind.LValueReference:
            return self.current_lval or "val"

        # Binary operation
        elif k == pyslang.ast.ExpressionKind.BinaryOp:
            left_str = self.transpile(expr.left)
            right_str = self.transpile(expr.right)
            op = expr.op

            # 1. Operators without direct C++ equivalent or requiring structural rewriting
            if op == pyslang.ast.BinaryOperator.Power:
                res = f"sv_pow(static_cast<uint64_t>({left_str}), static_cast<uint64_t>({right_str}))"
                return f"({res})" if self._is_parenthesized(expr) else res

            elif op == pyslang.ast.BinaryOperator.BinaryXnor:
                # XNOR: ~(a ^ b). Explicit parentheses protect against C++ operator precedence (~ higher than ^)
                res = f"(~({left_str} ^ {right_str}))"
                return res

            elif op == pyslang.ast.BinaryOperator.LogicalImplication:
                # a -> b equivalent to (!a || b)
                res = f"((!{left_str}) || {right_str})"
                return res

            elif op == pyslang.ast.BinaryOperator.LogicalEquivalence:
                # a <-> b equivalent to (!a == !b)
                res = f"((!{left_str}) == (!{right_str}))"
                return res

            # 2. Standard binary operators where SV and C++ share identical precedence and associativity
            op_map = {
                pyslang.ast.BinaryOperator.Add: "+",
                pyslang.ast.BinaryOperator.Subtract: "-",
                pyslang.ast.BinaryOperator.Multiply: "*",
                pyslang.ast.BinaryOperator.Divide: "/",
                pyslang.ast.BinaryOperator.Mod: "%",
                pyslang.ast.BinaryOperator.BinaryAnd: "&",
                pyslang.ast.BinaryOperator.BinaryOr: "|",
                pyslang.ast.BinaryOperator.BinaryXor: "^",
                pyslang.ast.BinaryOperator.LogicalShiftLeft: "<<",
                pyslang.ast.BinaryOperator.LogicalShiftRight: ">>",
                pyslang.ast.BinaryOperator.ArithmeticShiftLeft: "<<",
                pyslang.ast.BinaryOperator.ArithmeticShiftRight: ">>",
                pyslang.ast.BinaryOperator.Equality: "==",
                pyslang.ast.BinaryOperator.Inequality: "!=",
                pyslang.ast.BinaryOperator.CaseEquality: "==",
                pyslang.ast.BinaryOperator.CaseInequality: "!=",
                pyslang.ast.BinaryOperator.WildcardEquality: "==",
                pyslang.ast.BinaryOperator.WildcardInequality: "!=",
                pyslang.ast.BinaryOperator.GreaterThan: ">",
                pyslang.ast.BinaryOperator.GreaterThanEqual: ">=",
                pyslang.ast.BinaryOperator.LessThan: "<",
                pyslang.ast.BinaryOperator.LessThanEqual: "<=",
                pyslang.ast.BinaryOperator.LogicalAnd: "&&",
                pyslang.ast.BinaryOperator.LogicalOr: "||",
            }
            c_op = op_map.get(op, "/* unknown op */")
            res = f"{left_str} {c_op} {right_str}"
            if self._is_parenthesized(expr):
                return f"({res})"
            return res

        # Unary operation
        elif k == pyslang.ast.ExpressionKind.UnaryOp:
            op = expr.op
            operand_str = self.transpile(expr.operand)
            if op == pyslang.ast.UnaryOperator.Postincrement:
                return f"{operand_str}++"
            elif op == pyslang.ast.UnaryOperator.Preincrement:
                return f"++{operand_str}"
            elif op == pyslang.ast.UnaryOperator.Postdecrement:
                return f"{operand_str}--"
            elif op == pyslang.ast.UnaryOperator.Predecrement:
                return f"--{operand_str}"

            # Unary reduction operators: SV reduction operators convert to scalar checks / parity
            elif op == pyslang.ast.UnaryOperator.BitwiseOr:
                return f"({operand_str} != 0)"
            elif op == pyslang.ast.UnaryOperator.BitwiseNor:
                return f"({operand_str} == 0)"
            elif op == pyslang.ast.UnaryOperator.BitwiseXor:
                return f"((__builtin_parityll({operand_str}) & 1) != 0)"
            elif op == pyslang.ast.UnaryOperator.BitwiseXnor:
                return f"((__builtin_parityll({operand_str}) & 1) == 0)"
            elif op == pyslang.ast.UnaryOperator.BitwiseAnd:
                w = getattr(expr.operand.type, "bitWidth", 0)
                if 0 < w < 64:
                    mask = (1 << w) - 1
                    return f"(({operand_str} & 0x{mask:x}ULL) == 0x{mask:x}ULL)"
                return f"({operand_str} == ~0ULL)"
            elif op == pyslang.ast.UnaryOperator.BitwiseNand:
                w = getattr(expr.operand.type, "bitWidth", 0)
                if 0 < w < 64:
                    mask = (1 << w) - 1
                    return f"(({operand_str} & 0x{mask:x}ULL) != 0x{mask:x}ULL)"
                return f"({operand_str} != ~0ULL)"

            # Standard unary operators: preserve parentheses only if written in original SV source
            op_map = {
                pyslang.ast.UnaryOperator.LogicalNot: "!",
                pyslang.ast.UnaryOperator.BitwiseNot: "~",
                pyslang.ast.UnaryOperator.Plus: "+",
                pyslang.ast.UnaryOperator.Minus: "-",
            }
            c_op = op_map.get(op, "")
            res = f"{c_op}{operand_str}"
            if self._is_parenthesized(expr):
                return f"({res})"
            return res

        # Ternary conditional (cond ? true_expr : false_expr)
        elif k == pyslang.ast.ExpressionKind.ConditionalOp:
            cond_str = self.transpile(expr.conditions[0].expr)
            left_str = self.transpile(expr.left)
            right_str = self.transpile(expr.right)
            return f"({cond_str} ? {left_str} : {right_str})"

        # Element / Array select: a[i] or bit-select
        elif k == pyslang.ast.ExpressionKind.ElementSelect:
            base_str = self.transpile(expr.value)
            idx_str = self.transpile(expr.selector)
            if self._is_integral_select(expr):
                return f"(({base_str} >> ({idx_str})) & 1ULL)"
            return f"{base_str}[{idx_str}]"

        # Range select / bit slicing: a[63:32], a[base +: width], a[base -: width]
        elif k == pyslang.ast.ExpressionKind.RangeSelect:
            base_str = self.transpile(expr.value)
            lo_str, lo_is_zero, mask = self._get_range_lo_and_mask(expr)
            if lo_is_zero:
                return f"({base_str} & 0x{mask:x}ULL)"
            return f"(({base_str} >> {lo_str}) & 0x{mask:x}ULL)"

        # Concatenation: {a, b, c}
        elif k == pyslang.ast.ExpressionKind.Concatenation:
            return self._transpile_concat_operands(list(expr.operands))

        # Replication: {N{val}}
        elif k == pyslang.ast.ExpressionKind.Replication:
            # 1. Constant evaluation first if possible
            if hasattr(expr, "constant") and expr.constant:
                try:
                    val = int(expr.constant.convertToInt().value)
                    w = expr.type.bitWidth if hasattr(expr.type, "bitWidth") else 32
                    if val == 0:
                        return "0"
                    if w > 4 and val > 9:
                        return f"0x{val:x}"
                    return str(val)
                except Exception:
                    pass

            count = self.eval_int_val(expr.count) if hasattr(expr, "count") else 1
            if hasattr(expr, "concat") and expr.concat:
                if expr.concat.kind == pyslang.ast.ExpressionKind.Concatenation:
                    sub_ops = list(expr.concat.operands)
                else:
                    sub_ops = [expr.concat]
            else:
                sub_ops = []

            # Special optimization: single 1-bit operand repeated N times (sign extension / bit broadcast)
            if len(sub_ops) == 1 and getattr(sub_ops[0].type, "bitWidth", 0) == 1 and 1 < count <= 64:
                op_c = self.transpile(sub_ops[0])
                mask = (1 << count) - 1
                return f"(({op_c}) ? 0x{mask:x}ULL : 0ULL)"

            return self._transpile_concat_operands(sub_ops * count)

        # Hierarchical value
        elif k == pyslang.ast.ExpressionKind.HierarchicalValue:
            self.has_unsupported = True
            self.unsupported_reason = f"Hierarchical access ({expr.syntax})"
            return str(expr.syntax).strip()

        # Call Expression (function call, system task, or queue/array method)
        elif k == pyslang.ast.ExpressionKind.Call:
            sub = getattr(expr, "subroutine", None)
            # 1. System task / function or built-in container method
            if hasattr(sub, "subroutine"):
                sys_name = sub.subroutine.name
                if sys_name == "$fread":
                    tgt = self.transpile(expr.arguments[0])
                    fd = self.transpile(expr.arguments[1])
                    return f"mem_fread({tgt}, {fd})"
                elif sys_name == "$fgets":
                    str_var = self.transpile(expr.arguments[0])
                    fd = self.transpile(expr.arguments[1])
                    return f"mem_fgets({str_var}, {fd})"
                elif sys_name == "$fseek":
                    fd = self.transpile(expr.arguments[0])
                    offset = self.transpile(expr.arguments[1])
                    whence = self.transpile(expr.arguments[2])
                    return f"mem_fseek({fd}, {offset}, {whence})"
                elif sys_name == "$ftell":
                    fd = self.transpile(expr.arguments[0])
                    return f"mem_ftell({fd})"
                elif sys_name == "$feof":
                    fd = self.transpile(expr.arguments[0])
                    return f"mem_feof({fd})"
                elif sys_name == "$rewind":
                    fd = self.transpile(expr.arguments[0])
                    return f"mem_rewind({fd})"
                elif sys_name == "$fgetc":
                    fd = self.transpile(expr.arguments[0])
                    return f"mem_fgetc({fd})"
                elif sys_name == "$ungetc":
                    c = self.transpile(expr.arguments[0])
                    fd = self.transpile(expr.arguments[1])
                    return f"mem_ungetc({c}, {fd})"
                elif sys_name == "$ferror":
                    fd = self.transpile(expr.arguments[0])
                    str_var = self.transpile(expr.arguments[1]) if len(expr.arguments) > 1 else ""
                    return f"mem_ferror({fd}, {str_var})"
                elif sys_name == "$fflush":
                    fd = self.transpile(expr.arguments[0]) if expr.arguments else "0"
                    return f"mem_fflush({fd})"
                elif sys_name == "$fscanf":
                    fd = self.transpile(expr.arguments[0])
                    fmt = _normalize_sv_format(self.transpile(expr.arguments[1]))
                    args = ", ".join([f"&{self.transpile(a)}" for a in expr.arguments[2:]])
                    return f"mem_fscanf({fd}, {fmt}, {args})"
                elif sys_name == "$fopen":
                    path = self.transpile(expr.arguments[0])
                    mode = self.transpile(expr.arguments[1]) if len(expr.arguments) > 1 else '"r"'
                    return f"mem_fopen({path}, {mode})"
                elif sys_name == "$test$plusargs":
                    name = self.transpile(expr.arguments[0])
                    return f"sv_test_plusargs({name})"
                elif sys_name == "$value$plusargs":
                    fmt = self.transpile(expr.arguments[0])
                    if len(expr.arguments) > 1:
                        target = self.transpile(expr.arguments[1])
                        return f"sv_value_plusargs({fmt}, {target})"
                    return "0"
                elif sys_name == "$sformatf":
                    c_fmt, other_args = self._format_varargs(expr.arguments[0], expr.arguments[1:])
                    return f"sv_sformatf({c_fmt}{other_args})"
                elif sys_name == "$urandom_range":
                    args = [self.transpile(a) for a in expr.arguments]
                    return f"sv_urandom_range({', '.join(args)})"
                elif sys_name == "$urandom":
                    return "sv_urandom()"
                elif sys_name in ("$time", "$stime"):
                    if sys_name in self.var_mappings:
                        return self.var_mappings[sys_name]
                    if sys_name in self.task_mappings:
                        return f"{self.task_mappings[sys_name]}()"
                    return "0ULL" if sys_name == "$time" else "0"
                elif sys_name == "$realtime":
                    if sys_name in self.var_mappings:
                        return self.var_mappings[sys_name]
                    if sys_name in self.task_mappings:
                        return f"{self.task_mappings[sys_name]}()"
                    return "0.0"

                # Built-in container methods on queues / dynamic arrays
                elif sys_name in ("push_back", "push_front", "pop_back", "pop_front", "size", "delete", "insert", "sort", "rsort", "reverse"):
                    tgt = self.transpile(expr.arguments[0])
                    if sys_name == "size":
                        return f"static_cast<int32_t>({tgt}.size())"
                    elif sys_name == "pop_back":
                        return f"([&](){{ auto _v = {tgt}.back(); {tgt}.pop_back(); return _v; }}())"
                    elif sys_name == "pop_front":
                        return f"([&](){{ auto _v = {tgt}.front(); {tgt}.erase({tgt}.begin()); return _v; }}())"
                    elif sys_name == "push_back":
                        val = self.transpile(expr.arguments[1])
                        return f"{tgt}.push_back({val})"
                    elif sys_name == "push_front":
                        val = self.transpile(expr.arguments[1])
                        return f"{tgt}.insert({tgt}.begin(), {val})"
                    elif sys_name == "delete":
                        if len(expr.arguments) > 1:
                            idx = self.transpile(expr.arguments[1])
                            return f"{tgt}.erase({tgt}.begin() + ({idx}))"
                        return f"{tgt}.clear()"
                    elif sys_name == "insert":
                        idx = self.transpile(expr.arguments[1])
                        val = self.transpile(expr.arguments[2])
                        return f"{tgt}.insert({tgt}.begin() + ({idx}), {val})"
                    elif sys_name == "sort":
                        return f"std::sort({tgt}.begin(), {tgt}.end())"
                    elif sys_name == "rsort":
                        return f"std::sort({tgt}.rbegin(), {tgt}.rend())"
                    elif sys_name == "reverse":
                        return f"std::reverse({tgt}.begin(), {tgt}.end())"
                else:
                    self.has_unsupported = True
                    self.unsupported_reason = f"System call ({sys_name})"
                    return "0"

            elif hasattr(sub, "name"):
                f_name = sub.name
                is_mapped = f_name in self.task_mappings
                args = []
                for a in expr.arguments:
                    if a.kind == pyslang.ast.ExpressionKind.Assignment:
                        actual_arg = self.transpile(a.left)
                        if is_mapped and not getattr(a.left.type, "isUnpackedArray", False):
                            args.append(f"&{actual_arg}")
                        else:
                            args.append(actual_arg)
                    else:
                        args.append(self.transpile(a))
                target_func = self.task_mappings.get(f_name, f_name)
                return f"{target_func}({', '.join(args)})"
            else:
                self.has_unsupported = True
                self.unsupported_reason = "Unknown subroutine call"
                return "0"

        # Generic fallback
        else:
            self.has_unsupported = True
            self.unsupported_reason = f"Expression kind ({k.name})"
            return str(expr.syntax).strip()


class CppStatementTranspiler:
    """Transpiles SystemVerilog statements (AST) into clean, modern C++ statements."""

    def __init__(self, task_mappings=None, var_mappings=None, out_params=None, warnings=None, source_manager=None, visited_comments=None, discovered_macros=None, cpp_std="c++17"):
        self.task_mappings = task_mappings or {}
        self.var_mappings = var_mappings or {}
        self.out_params = out_params or set()
        self.warnings = warnings if warnings is not None else []
        self.source_manager = source_manager
        self.visited_comments = visited_comments if visited_comments is not None else set()
        self.loop_id = 0
        self.discovered_macros = discovered_macros if discovered_macros is not None else {}
        self.cpp_std = cpp_std
        self.expr_transpiler = CppExprTranspiler(
            task_mappings,
            var_mappings,
            out_params,
            source_manager=self.source_manager,
            discovered_macros=self.discovered_macros,
            cpp_std=self.cpp_std
        )

    def extract_leading_comments(self, syntax_node):
        comments = []
        if syntax_node is None:
            return comments
        tok = getattr(syntax_node, "getFirstToken", lambda: None)()
        if tok:
            for i, tr in enumerate(tok.trivia):
                if "Comment" in str(tr.kind):
                    buf_id = getattr(getattr(tok.location, "buffer", None), "id", 0)
                    offset = getattr(tok.location, "offset", 0)
                    raw_text = tr.getRawText()
                    key = (buf_id, offset, i)
                    if key not in self.visited_comments:
                        self.visited_comments.add(key)
                        comments.append(raw_text.strip())
        return comments

    def clean_comment(self, text):
        return str(text).replace("/*", "/[*]").replace("*/", "[*]/")

    def _log_warning(self, stmt, kind_desc):
        syntax_str = str(stmt.syntax).strip() if hasattr(stmt, "syntax") and stmt.syntax else str(stmt)
        syntax_clean = " ".join(syntax_str.split())
        loc_str = ""
        if self.source_manager and hasattr(stmt, "sourceRange") and stmt.sourceRange:
            try:
                loc = stmt.sourceRange.start
                fname = self.source_manager.getFileName(loc)
                line = self.source_manager.getLineNumber(loc)
                loc_str = f" (at {fname}:{line})"
            except Exception:
                pass
        msg = f"{kind_desc}: {self.clean_comment(syntax_clean)}{loc_str}"
        self.warnings.append(msg)
        return syntax_clean

    def transpile_type(self, sv_type):
        """Map SV types to modern C++ types."""
        return sv_type_to_cpp(sv_type)

    def _is_for_decl_block(self, stmt):
        """Check if a BlockStatement is the implicit scope created by pyslang for a ForLoopStatement with inline variable declarations."""
        if stmt is None or stmt.kind != pyslang.ast.StatementKind.Block:
            return False
        syn_kind = getattr(getattr(stmt, "syntax", None), "kind", None)
        if syn_kind == pyslang.syntax.SyntaxKind.ForLoopStatement:
            return True
        body = stmt.body
        sub_stmts = body.list if hasattr(body, "list") else [body]
        if (len(sub_stmts) == 2 and
                sub_stmts[0].kind == pyslang.ast.StatementKind.VariableDeclaration and
                sub_stmts[1].kind == pyslang.ast.StatementKind.ForLoop and
                not getattr(sub_stmts[1], "initializers", [])):
            return True
        return False

    def _ends_with_jump(self, stmt):
        """Check if a statement (e.g. a switch case branch) unconditionally ends with return, break, or continue."""
        if stmt is None:
            return False
        k = stmt.kind
        if k in (pyslang.ast.StatementKind.Return, pyslang.ast.StatementKind.Break, pyslang.ast.StatementKind.Continue):
            return True
        if k == pyslang.ast.StatementKind.Block:
            return self._ends_with_jump(stmt.body)
        if k == pyslang.ast.StatementKind.List:
            return bool(stmt.list) and self._ends_with_jump(stmt.list[-1])
        return False

    def transpile_body(self, body_stmt, indent_level):
        """Transpile a compound statement body without redundant extra block braces."""
        if body_stmt is None:
            return []
        if body_stmt.kind == pyslang.ast.StatementKind.Block and not self._is_for_decl_block(body_stmt):
            return self.transpile_statement(body_stmt.body, indent_level)
        return self.transpile_statement(body_stmt, indent_level)

    def transpile_statement(self, stmt, indent_level=1):
        lines = []
        indent = "    " * indent_level

        if stmt is None:
            return lines

        syntax_node = None
        if hasattr(stmt, "syntax") and stmt.syntax:
            syntax_node = stmt.syntax
        elif hasattr(stmt, "symbol") and hasattr(stmt.symbol, "syntax") and stmt.symbol.syntax:
            syntax_node = getattr(stmt.symbol.syntax, "parent", stmt.symbol.syntax)

        if syntax_node:
            leading_comments = self.extract_leading_comments(syntax_node)
            for comm in leading_comments:
                for c_line in comm.splitlines():
                    c_clean = c_line.strip()
                    if c_clean:
                        lines.append(f"{indent}{c_clean}")

        k = stmt.kind

        # Statement List
        if k == pyslang.ast.StatementKind.List:
            for s in stmt.list:
                lines.extend(self.transpile_statement(s, indent_level))

        # Block (begin ... end or implicit ForLoop variable declaration scope)
        elif k == pyslang.ast.StatementKind.Block:
            if self._is_for_decl_block(stmt):
                body = stmt.body
                sub_stmts = list(body.list) if hasattr(body, "list") else [body]
                loop_vars = [s.symbol for s in sub_stmts[:-1] if s.kind == pyslang.ast.StatementKind.VariableDeclaration]
                for_stmt = sub_stmts[-1]
                lines.extend(self.transpile_for_loop(loop_vars, for_stmt, indent_level))
            else:
                lines.append(f"{indent}{{")
                lines.extend(self.transpile_statement(stmt.body, indent_level + 1))
                lines.append(f"{indent}}}")

        # Variable Declaration inside procedural scope
        elif k == pyslang.ast.StatementKind.VariableDeclaration:
            var_sym = stmt.symbol
            cpp_type, arr_dim = self.transpile_type(var_sym.type)
            init_str = ""
            is_struct = getattr(var_sym.type, "isStruct", False) or getattr(var_sym.type, "isAlias", False)
            is_string = getattr(var_sym.type, "isString", False)
            is_vector = "std::vector" in cpp_type

            if var_sym.initializer:
                self.expr_transpiler.has_unsupported = False
                init_val = self.expr_transpiler.transpile(var_sym.initializer)
                if self.expr_transpiler.has_unsupported:
                    self._log_warning(stmt, f"Unsupported initializer ({self.expr_transpiler.unsupported_reason}) replaced with default")
                    init_str = " = {}" if (arr_dim or is_struct or is_vector) else ("" if is_string else " = 0")
                else:
                    init_str = f" = {init_val}"
            elif is_string or is_vector:
                init_str = ""
            elif arr_dim or is_struct:
                init_str = " = {}"
            else:
                init_str = " = 0"

            if arr_dim:
                dim_suffix = arr_dim if arr_dim.startswith("[") else f"[{arr_dim}]"
                lines.append(f"{indent}{cpp_type} {var_sym.name}{dim_suffix}{init_str};")
            else:
                lines.append(f"{indent}{cpp_type} {var_sym.name}{init_str};")

        # Expression Statement (assignment or call)
        elif k == pyslang.ast.StatementKind.ExpressionStatement:
            expr = stmt.expr
            if expr.kind == pyslang.ast.ExpressionKind.Assignment:
                if expr.right.kind == pyslang.ast.ExpressionKind.NewArray:
                    self.expr_transpiler.has_unsupported = False
                    left_c = self.expr_transpiler.transpile(expr.left)
                    size_c = self.expr_transpiler.transpile(expr.right.sizeExpr) if hasattr(expr.right, "sizeExpr") and expr.right.sizeExpr else "0"
                    lines.append(f"{indent}{left_c}.resize({size_c});")
                    return lines

                self.expr_transpiler.has_unsupported = False
                assign_c = self.expr_transpiler.transpile(expr)
                if self.expr_transpiler.has_unsupported:
                    syntax_clean = self._log_warning(stmt, f"Unsupported assignment ({self.expr_transpiler.unsupported_reason}) ignored")
                    lines.append(f"{indent}/* [WARNING: Unsupported assignment ignored] {self.clean_comment(syntax_clean)} */")
                    return lines

                lines.append(f"{indent}{assign_c};")

            elif expr.kind == pyslang.ast.ExpressionKind.UnaryOp:
                self.expr_transpiler.has_unsupported = False
                u_str = self.expr_transpiler.transpile(expr)
                if self.expr_transpiler.has_unsupported:
                    syntax_clean = self._log_warning(stmt, "Unsupported unary statement ignored")
                    lines.append(f"{indent}/* [WARNING: Unsupported unary statement ignored] {self.clean_comment(syntax_clean)} */")
                else:
                    lines.append(f"{indent}{u_str};")

            elif expr.kind == pyslang.ast.ExpressionKind.Call:
                self.expr_transpiler.has_unsupported = False
                call_line = self.transpile_call(expr)
                if self.expr_transpiler.has_unsupported or not call_line:
                    reason = self.expr_transpiler.unsupported_reason or "call ignored"
                    syntax_clean = self._log_warning(stmt, f"Unsupported call ({reason}) ignored")
                    lines.append(f"{indent}/* [WARNING: Unsupported call ignored] {self.clean_comment(syntax_clean)} */")
                else:
                    lines.append(f"{indent}{call_line};")

            else:
                syntax_clean = self._log_warning(stmt, "Unsupported expression statement ignored")
                lines.append(f"{indent}/* [WARNING: Unsupported expression statement ignored] {self.clean_comment(syntax_clean)} */")

        # Conditional (if / else)
        elif k == pyslang.ast.StatementKind.Conditional:
            self.expr_transpiler.has_unsupported = False
            cond_c = self.expr_transpiler.transpile(stmt.conditions[0].expr)
            if self.expr_transpiler.has_unsupported:
                syntax_clean = self._log_warning(stmt, f"Unsupported condition ({self.expr_transpiler.unsupported_reason}) ignored")
                lines.append(f"{indent}/* [WARNING: Unsupported conditional ignored] {self.clean_comment(syntax_clean)} */")
                return lines

            lines.append(f"{indent}if ({cond_c}) {{")
            lines.extend(self.transpile_body(stmt.ifTrue, indent_level + 1))

            curr = getattr(stmt, "ifFalse", None)
            while curr and curr.kind == pyslang.ast.StatementKind.Conditional:
                self.expr_transpiler.has_unsupported = False
                cond_else = self.expr_transpiler.transpile(curr.conditions[0].expr)
                lines.append(f"{indent}}} else if ({cond_else}) {{")
                lines.extend(self.transpile_body(curr.ifTrue, indent_level + 1))
                curr = getattr(curr, "ifFalse", None)

            if curr:
                lines.append(f"{indent}}} else {{")
                lines.extend(self.transpile_body(curr, indent_level + 1))

            lines.append(f"{indent}}}")

        # Case statement -> switch / case
        elif k == pyslang.ast.StatementKind.Case:
            self.expr_transpiler.has_unsupported = False
            expr_c = self.expr_transpiler.transpile(stmt.expr)
            if self.expr_transpiler.has_unsupported:
                syntax_clean = self._log_warning(stmt, f"Unsupported case expression ({self.expr_transpiler.unsupported_reason}) ignored")
                lines.append(f"{indent}/* [WARNING: Unsupported case statement ignored] {self.clean_comment(syntax_clean)} */")
                return lines
            lines.append(f"{indent}switch ({expr_c}) {{")
            for item in stmt.items:
                for exp in item.expressions:
                    exp_val = self.expr_transpiler.transpile(exp)
                    lines.append(f"{indent}    case {exp_val}:")
                item_lines = self.transpile_statement(item.stmt, indent_level + 2)
                lines.extend(item_lines)
                if not self._ends_with_jump(item.stmt):
                    lines.append(f"{indent}        break;")
            if stmt.defaultCase:
                lines.append(f"{indent}    default:")
                def_lines = self.transpile_statement(stmt.defaultCase, indent_level + 2)
                lines.extend(def_lines)
                if not self._ends_with_jump(stmt.defaultCase):
                    lines.append(f"{indent}        break;")
            lines.append(f"{indent}}}")

        # While Loop
        elif k == pyslang.ast.StatementKind.WhileLoop:
            self.expr_transpiler.has_unsupported = False
            cond_c = self.expr_transpiler.transpile(stmt.cond)
            if self.expr_transpiler.has_unsupported:
                syntax_clean = self._log_warning(stmt, f"Unsupported while loop condition ({self.expr_transpiler.unsupported_reason}) ignored")
                lines.append(f"{indent}/* [WARNING: Unsupported while loop ignored] {self.clean_comment(syntax_clean)} */")
                return lines
            if hasattr(stmt.body, "kind") and stmt.body.kind == pyslang.ast.StatementKind.Block and not self._is_for_decl_block(stmt.body):
                lines.append(f"{indent}while ({cond_c}) {{")
                lines.extend(self.transpile_body(stmt.body, indent_level + 1))
                lines.append(f"{indent}}}")
            else:
                lines.append(f"{indent}while ({cond_c})")
                lines.extend(self.transpile_statement(stmt.body, indent_level + 1))

        # Repeat Loop
        elif k == pyslang.ast.StatementKind.RepeatLoop:
            self.expr_transpiler.has_unsupported = False
            count_c = self.expr_transpiler.transpile(stmt.count)
            if self.expr_transpiler.has_unsupported:
                syntax_clean = self._log_warning(stmt, f"Unsupported repeat loop count ({self.expr_transpiler.unsupported_reason}) ignored")
                lines.append(f"{indent}/* [WARNING: Unsupported repeat loop ignored] {self.clean_comment(syntax_clean)} */")
                return lines
            loop_var = f"_rep_{self.loop_id}"
            self.loop_id += 1
            if hasattr(stmt.body, "kind") and stmt.body.kind == pyslang.ast.StatementKind.Block and not self._is_for_decl_block(stmt.body):
                lines.append(f"{indent}for (int32_t {loop_var} = 0; {loop_var} < {count_c}; {loop_var}++) {{")
                lines.extend(self.transpile_body(stmt.body, indent_level + 1))
                lines.append(f"{indent}}}")
            else:
                lines.append(f"{indent}for (int32_t {loop_var} = 0; {loop_var} < {count_c}; {loop_var}++)")
                lines.extend(self.transpile_statement(stmt.body, indent_level + 1))

        # Forever Loop
        elif k == pyslang.ast.StatementKind.ForeverLoop:
            if hasattr(stmt.body, "kind") and stmt.body.kind == pyslang.ast.StatementKind.Block and not self._is_for_decl_block(stmt.body):
                lines.append(f"{indent}while (true) {{")
                lines.extend(self.transpile_body(stmt.body, indent_level + 1))
                lines.append(f"{indent}}}")
            else:
                lines.append(f"{indent}while (true)")
                lines.extend(self.transpile_statement(stmt.body, indent_level + 1))

        # Do-While Loop
        elif k == pyslang.ast.StatementKind.DoWhileLoop:
            self.expr_transpiler.has_unsupported = False
            cond_c = self.expr_transpiler.transpile(stmt.cond)
            if self.expr_transpiler.has_unsupported:
                syntax_clean = self._log_warning(stmt, f"Unsupported do-while loop condition ({self.expr_transpiler.unsupported_reason}) ignored")
                lines.append(f"{indent}/* [WARNING: Unsupported do-while loop ignored] {self.clean_comment(syntax_clean)} */")
                return lines
            if hasattr(stmt.body, "kind") and stmt.body.kind == pyslang.ast.StatementKind.Block and not self._is_for_decl_block(stmt.body):
                lines.append(f"{indent}do {{")
                lines.extend(self.transpile_body(stmt.body, indent_level + 1))
                lines.append(f"{indent}}} while ({cond_c});")
            else:
                lines.append(f"{indent}do")
                lines.extend(self.transpile_statement(stmt.body, indent_level + 1))
                lines.append(f"{indent}while ({cond_c});")

        # For Loop
        elif k == pyslang.ast.StatementKind.ForLoop:
            lines.extend(self.transpile_for_loop(None, stmt, indent_level))

        # Break / Continue
        elif k == pyslang.ast.StatementKind.Break:
            lines.append(f"{indent}break;")
        elif k == pyslang.ast.StatementKind.Continue:
            lines.append(f"{indent}continue;")

        # Return statement
        elif k == pyslang.ast.StatementKind.Return:
            if hasattr(stmt, "expr") and stmt.expr:
                self.expr_transpiler.has_unsupported = False
                ret_val = self.expr_transpiler.transpile(stmt.expr)
                if self.expr_transpiler.has_unsupported:
                    syntax_clean = self._log_warning(stmt, f"Unsupported return expression ({self.expr_transpiler.unsupported_reason}) ignored")
                    lines.append(f"{indent}/* [WARNING: Unsupported return ignored] {self.clean_comment(syntax_clean)} */")
                    return lines
                lines.append(f"{indent}return {ret_val};")
            else:
                lines.append(f"{indent}return;")

        # Empty statement
        elif k == pyslang.ast.StatementKind.Empty:
            pass

        # Timed Statement (#delay, @event)
        elif k == pyslang.ast.StatementKind.Timed:
            syntax_clean = self._log_warning(stmt, "Simulation timing construct ignored")
            lines.append(f"{indent}/* [WARNING: Simulation timing construct ignored] {self.clean_comment(syntax_clean)} */")
            if hasattr(stmt, "stmt") and stmt.stmt and stmt.stmt.kind != pyslang.ast.StatementKind.Empty:
                lines.extend(self.transpile_statement(stmt.stmt, indent_level))

        # Wait Statement
        elif k == pyslang.ast.StatementKind.Wait:
            syntax_clean = self._log_warning(stmt, "Simulation wait construct ignored")
            lines.append(f"{indent}/* [WARNING: Simulation wait construct ignored] {self.clean_comment(syntax_clean)} */")
            if hasattr(stmt, "stmt") and stmt.stmt and stmt.stmt.kind != pyslang.ast.StatementKind.Empty:
                lines.extend(self.transpile_statement(stmt.stmt, indent_level))

        # WaitFork / WaitOrder
        elif k in (pyslang.ast.StatementKind.WaitFork, pyslang.ast.StatementKind.WaitOrder):
            syntax_clean = self._log_warning(stmt, f"Simulation {k.name} ignored")
            lines.append(f"{indent}/* [WARNING: Simulation {k.name} ignored] {self.clean_comment(syntax_clean)} */")

        # Disable / DisableFork
        elif k in (pyslang.ast.StatementKind.Disable, pyslang.ast.StatementKind.DisableFork):
            syntax_clean = self._log_warning(stmt, f"Simulation {k.name} ignored")
            lines.append(f"{indent}/* [WARNING: Simulation {k.name} ignored] {self.clean_comment(syntax_clean)} */")

        # EventTrigger
        elif k == pyslang.ast.StatementKind.EventTrigger:
            syntax_clean = self._log_warning(stmt, "Simulation event trigger ignored")
            lines.append(f"{indent}/* [WARNING: Simulation event trigger ignored] {self.clean_comment(syntax_clean)} */")

        # ProceduralAssign / ProceduralDeassign
        elif k in (pyslang.ast.StatementKind.ProceduralAssign, pyslang.ast.StatementKind.ProceduralDeassign):
            syntax_clean = self._log_warning(stmt, f"Simulation {k.name} ignored")
            lines.append(f"{indent}/* [WARNING: Simulation {k.name} ignored] {self.clean_comment(syntax_clean)} */")

        # Assertion Statements
        elif k in (pyslang.ast.StatementKind.ImmediateAssertion, pyslang.ast.StatementKind.ConcurrentAssertion):
            syntax_clean = self._log_warning(stmt, f"Simulation assertion ({k.name}) ignored")
            lines.append(f"{indent}/* [WARNING: Simulation assertion ignored] {self.clean_comment(syntax_clean)} */")

        # Fallback
        else:
            syntax_clean = self._log_warning(stmt, f"Unsupported construct ({k.name}) ignored")
            lines.append(f"{indent}/* [WARNING: Unsupported SystemVerilog construct ({k.name}) ignored] {self.clean_comment(syntax_clean)} */")

        return lines

    def transpile_for_loop(self, loop_vars, for_stmt, indent_level):
        lines = []
        indent = "    " * indent_level

        self.expr_transpiler.has_unsupported = False
        if loop_vars:
            if not isinstance(loop_vars, (list, tuple)):
                loop_vars = [loop_vars]
            cpp_type, _ = self.transpile_type(loop_vars[0].type)
            decl_parts = []
            for lv in loop_vars:
                init_c = self.expr_transpiler.transpile(lv.initializer) if lv.initializer else "0"
                decl_parts.append(f"{lv.name} = {init_c}")
            for_header = f"{cpp_type} {', '.join(decl_parts)}"
        else:
            inits = getattr(for_stmt, "initializers", [])
            for_header = ", ".join([self.expr_transpiler.transpile(i) for i in inits]) if inits else ""

        cond_c = self.expr_transpiler.transpile(for_stmt.stopExpr) if hasattr(for_stmt, "stopExpr") else ""
        step_c = ", ".join([self.expr_transpiler.transpile(s) for s in for_stmt.steps]) if hasattr(for_stmt, "steps") else ""

        if self.expr_transpiler.has_unsupported:
            syntax_clean = self._log_warning(for_stmt, f"Unsupported for-loop condition/step ({self.expr_transpiler.unsupported_reason}) ignored")
            lines.append(f"{indent}/* [WARNING: Unsupported for-loop ignored] {self.clean_comment(syntax_clean)} */")
            return lines

        if hasattr(for_stmt.body, "kind") and for_stmt.body.kind == pyslang.ast.StatementKind.Block and not self._is_for_decl_block(for_stmt.body):
            lines.append(f"{indent}for ({for_header}; {cond_c}; {step_c}) {{")
            lines.extend(self.transpile_body(for_stmt.body, indent_level + 1))
            lines.append(f"{indent}}}")
        else:
            lines.append(f"{indent}for ({for_header}; {cond_c}; {step_c})")
            lines.extend(self.transpile_statement(for_stmt.body, indent_level + 1))
        return lines

    def transpile_call(self, call_expr):
        sub = getattr(call_expr, "subroutine", None)
        if hasattr(sub, "subroutine"):
            sys_name = sub.subroutine.name
            if sys_name in ("$display", "$write", "$info", "$warning", "$error"):
                if call_expr.arguments:
                    c_fmt, other_args = self.expr_transpiler._format_varargs(call_expr.arguments[0], call_expr.arguments[1:])
                    if self.expr_transpiler.has_unsupported:
                        return ""
                    if sys_name != "$write" and c_fmt.endswith('"') and not c_fmt.endswith('\\n"'):
                        c_fmt = c_fmt[:-1] + '\\n"'
                    return f"printf({c_fmt}{other_args})"
                return 'printf("\\n")' if sys_name != "$write" else ""

            elif sys_name == "$fatal":
                if len(call_expr.arguments) > 1:
                    c_msg, other_args = self.expr_transpiler._format_varargs(call_expr.arguments[1], call_expr.arguments[2:])
                else:
                    c_msg, other_args = '"Simulation fatal error"', ""
                if self.expr_transpiler.has_unsupported:
                    return ""
                return f'printf("FATAL: " {c_msg} "\\n"{other_args}); exit(1)'

            elif sys_name in ("$finish", "$stop"):
                return "exit(0)"

            elif sys_name == "$sformat":
                target = self.expr_transpiler.transpile(call_expr.arguments[0])
                c_fmt, other_args = self.expr_transpiler._format_varargs(call_expr.arguments[1], call_expr.arguments[2:])
                if self.expr_transpiler.has_unsupported:
                    return ""
                return f"sv_sformat({target}, {c_fmt}{other_args})"

            elif sys_name == "$fclose":
                fd = self.expr_transpiler.transpile(call_expr.arguments[0])
                return "" if self.expr_transpiler.has_unsupported else f"mem_fclose({fd})"

            elif sys_name in ("$readmemh", "$readmemb"):
                path = self.expr_transpiler.transpile(call_expr.arguments[0])
                target = self.expr_transpiler.transpile(call_expr.arguments[1])
                fn = "mem_readmemh" if sys_name == "$readmemh" else "mem_readmemb"
                if self.expr_transpiler.has_unsupported:
                    return ""
                return f"{fn}({path}, {target}, sizeof({target}[0]), sizeof({target}) / sizeof({target}[0]))"

            elif sys_name in ("$fdisplay", "$fwrite"):
                fd = self.expr_transpiler.transpile(call_expr.arguments[0])
                fn = "mem_fdisplay" if sys_name == "$fdisplay" else "mem_fwrite"
                if len(call_expr.arguments) > 1:
                    c_fmt, other_args = self.expr_transpiler._format_varargs(call_expr.arguments[1], call_expr.arguments[2:])
                    if self.expr_transpiler.has_unsupported:
                        return ""
                    if sys_name == "$fdisplay" and c_fmt.endswith('"') and not c_fmt.endswith('\\n"'):
                        c_fmt = c_fmt[:-1] + '\\n"'
                    return f"{fn}({fd}, {c_fmt}{other_args})"
                return f'mem_fdisplay({fd}, "\\n")' if sys_name == "$fdisplay" else ""

            elif sys_name in ("$srandom", "srandom"):
                arg = self.expr_transpiler.transpile(call_expr.arguments[0]) if call_expr.arguments else "0"
                return "" if self.expr_transpiler.has_unsupported else f"sv_srandom({arg})"

            elif sys_name == "pop_front":
                tgt = self.expr_transpiler.transpile(call_expr.arguments[0])
                return "" if self.expr_transpiler.has_unsupported else f"{tgt}.erase({tgt}.begin())"

            elif sys_name == "pop_back":
                tgt = self.expr_transpiler.transpile(call_expr.arguments[0])
                return "" if self.expr_transpiler.has_unsupported else f"{tgt}.pop_back()"

            # Delegate all other system functions & container methods to expr_transpiler
            res = self.expr_transpiler.transpile(call_expr)
            if self.expr_transpiler.has_unsupported:
                if self.expr_transpiler.unsupported_reason.startswith("System call"):
                    self.expr_transpiler.unsupported_reason = f"System task ({sys_name})"
                return ""
            return res

        elif hasattr(sub, "name"):
            res = self.expr_transpiler.transpile(call_expr)
            return "" if self.expr_transpiler.has_unsupported else res

        self.expr_transpiler.has_unsupported = True
        self.expr_transpiler.unsupported_reason = "Unknown call target"
        return ""


class SV2CppAnalyzer:
    """
    Core analyzer using pyslang Driver to handle EDA command line options (-f, +define+, etc.),
    build AST, discover initial blocks, compute reachability graph, and transpile to modern C++.
    """

    def __init__(self, slang_args, task_mappings=None, var_mappings=None, embed_mode="auto", workdir=".", extra_declarations=None, embed_threshold_a=256, embed_threshold_b=1024, cpp_std="c++17"):
        self.slang_args = slang_args
        self.task_mappings = task_mappings or {}
        self.var_mappings = var_mappings or {}
        self.embed_mode = embed_mode or "auto"
        self.workdir = workdir or "."
        self.extra_declarations = extra_declarations or []
        self.embed_threshold_a = int(embed_threshold_a) if embed_threshold_a is not None else 256
        self.embed_threshold_b = int(embed_threshold_b) if embed_threshold_b is not None else 1024
        self.cpp_std = cpp_std

        self.driver = None
        self.compilation = None
        self.root = None

        # Discovered elements
        self.initial_processes = []
        self.all_subroutines = {}
        self.module_parameters = {}
        self.module_variables = {}
        self.struct_definitions = {}
        self.include_files = {}
        self.discovered_macros = {}
        self.macro_definition_order = {}

        # Reachability graph
        self.selected_process = None
        self.reachable_subs = set()
        self.mapped_leafs = set()
        self.needed_codegen = set()
        self.reachable_sys_calls = set()
        self.detected_file_ops = set()
        self.referenced_module_vars = set()
        self.referenced_parameters = set()
        self.referenced_structs = set()
        self.discovered_input_paths = []
        self.discovered_output_paths = []
        self.unresolved_dynamic_paths = []
        self.unconvertible_warnings = []
        self.visited_comment_keys = set()
        self.source_manager = None
        self._cached_cpp_code = None

    def _get_var_location_key(self, v_name):
        """Helper to sort variables by their source declaration order (buffer ID, offset)."""
        sym = self.module_variables.get(v_name)
        if sym and sym.location:
            loc = sym.location
            b_id = loc.buffer.id if hasattr(loc.buffer, "id") else 0
            return (b_id, loc.offset)
        return (0, 0)

    def _get_macro_location_key(self, m_name):
        """Helper to sort macros by their source definition order (buffer ID, offset)."""
        return self.macro_definition_order.get(m_name, (float("inf"), 0))

    def _get_sub_location_key(self, sub_name):
        """Helper to sort subroutines by their source declaration order (buffer ID, offset)."""
        info = self.all_subroutines.get(sub_name)
        sym = info.get("symbol") if info else None
        if sym and sym.location:
            loc = sym.location
            b_id = loc.buffer.id if hasattr(loc.buffer, "id") else 0
            return (b_id, loc.offset)
        return (0, 0)

    def compile(self):
        """Compile SystemVerilog sources using slang native Driver."""
        self.driver = pyslang.driver.Driver()
        self.driver.addStandardArgs()

        cmd_str = shlex.join(["slang"] + self.slang_args)
        self.driver.parseCommandLine(cmd_str)

        if not self.driver.processOptions():
            self.driver.reportDiagnostics(True)
            raise RuntimeError("slang failed to process compilation options")

        if not self.driver.parseAllSources():
            self.driver.reportDiagnostics(True)
            raise RuntimeError("slang failed to parse source files")

        self.compilation = self.driver.createCompilation()
        self.source_manager = self.compilation.sourceManager
        self.root = self.compilation.getRoot()

    def discover_elements(self):
        self.discover_include_files()

        # Collect all scopes to inspect: top instances, packages, and compilation units
        scopes = [top.body for top in self.root.topInstances]
        if hasattr(self.compilation, "getPackages"):
            for pkg in self.compilation.getPackages():
                if pkg.name != "std":
                    scopes.append(pkg)
        for member in self.root:
            if str(member.kind) == "SymbolKind.CompilationUnit":
                scopes.append(member)

        stmt_transpiler_helper = CppStatementTranspiler()

        for scope in scopes:
            # 1. Discover Typedef Structs
            for member in scope:
                if str(member.kind) == "SymbolKind.TypeAlias":
                    target_t = member.targetType.type
                    if getattr(target_t, "isStruct", False):
                        fields = []
                        for f in target_t:
                            f_type, f_dim = stmt_transpiler_helper.transpile_type(f.type)
                            fields.append({
                                "name": f.name,
                                "type": f_type,
                                "dim": f_dim
                            })
                        loc = member.location
                        buf = loc.buffer if loc else None
                        buf_kind = self.source_manager.getBufferKind(buf) if (self.source_manager and buf) else None
                        is_inc = (buf_kind == pyslang.BufferKind.IncludeFile)
                        inc_raw_name = Path(self.source_manager.getRawFileName(buf)).name if (is_inc and self.source_manager and buf) else None
                        origin_str = f"{self.source_manager.getFileName(loc)}:{self.source_manager.getLineNumber(loc)}" if loc else ""

                        self.struct_definitions[member.name] = {
                            "fields": fields,
                            "is_included": is_inc,
                            "include_name": inc_raw_name,
                            "origin": origin_str
                        }
                        if is_inc and inc_raw_name in self.include_files:
                            self.include_files[inc_raw_name]["structs"][member.name] = fields

            # 2. Discover Parameters & Localparams
            for member in scope:
                if str(member.kind) == "SymbolKind.Parameter":
                    val = member.value
                    val_str = "0"
                    try:
                        if hasattr(val, "convertToReal") and "real" in str(member.type).lower():
                            val_str = str(val.convertToReal())
                        elif hasattr(val, "convertToInt"):
                            int_val = int(val.convertToInt().value)
                            val_str = f"{int_val}ULL" if int_val > 0xffffffff else str(int_val)
                    except Exception:
                        val_str = str(val)

                    loc = member.location
                    buf = loc.buffer if loc else None
                    buf_kind = self.source_manager.getBufferKind(buf) if (self.source_manager and buf) else None
                    is_inc = (buf_kind == pyslang.BufferKind.IncludeFile)
                    inc_raw_name = Path(self.source_manager.getRawFileName(buf)).name if (is_inc and self.source_manager and buf) else None
                    origin_str = f"{self.source_manager.getFileName(loc)}:{self.source_manager.getLineNumber(loc)}" if loc else ""

                    self.module_parameters[member.name] = {
                        "val": val_str,
                        "is_included": is_inc,
                        "include_name": inc_raw_name,
                        "origin": origin_str
                    }
                    if is_inc and inc_raw_name in self.include_files:
                        self.include_files[inc_raw_name]["parameters"][member.name] = val_str

            # 3. Discover Module-level Variables
            for member in scope:
                if str(member.kind) in ("SymbolKind.Variable", "SymbolKind.Net"):
                    self.module_variables[member.name] = member
                    loc = member.location
                    buf = loc.buffer if loc else None
                    buf_kind = self.source_manager.getBufferKind(buf) if (self.source_manager and buf) else None
                    is_inc = (buf_kind == pyslang.BufferKind.IncludeFile)
                    inc_raw_name = Path(self.source_manager.getRawFileName(buf)).name if (is_inc and self.source_manager and buf) else None
                    if is_inc and inc_raw_name in self.include_files:
                        self.include_files[inc_raw_name]["variables"][member.name] = member

            # 4. Discover Subroutines
            for member in scope:
                if str(member.kind) == "SymbolKind.Subroutine":
                    name = member.name
                    if str(member.returnType) == "void":
                        ret_type = "void"
                    else:
                        ret_type, _ = stmt_transpiler_helper.transpile_type(member.returnType)
                    args = []
                    for arg in member.arguments:
                        direction = str(arg.direction)
                        base_type, arr_dim = stmt_transpiler_helper.transpile_type(arg.type)
                        if getattr(arg.type, "isString", False):
                            if "Out" in direction or "Ref" in direction:
                                cpp_type = "std::string&"
                            else:
                                cpp_type = "const std::string&"
                            arg_suffix = ""
                        elif arr_dim:
                            dim_suffix = arr_dim if arr_dim.startswith("[") else f"[{arr_dim}]"
                            cpp_type = base_type
                            arg_suffix = dim_suffix
                        else:
                            if "Out" in direction or "InOut" in direction or "Ref" in direction:
                                cpp_type = f"{base_type}&"
                            else:
                                cpp_type = base_type
                            arg_suffix = ""
                        args.append({
                            "name": arg.name + arg_suffix,
                            "direction": direction,
                            "c_type": cpp_type
                        })

                    sub_loc = member.location
                    origin_file = self.source_manager.getFileName(sub_loc) if self.source_manager else ""
                    origin_line = self.source_manager.getLineNumber(sub_loc) if self.source_manager else 0
                    buf = sub_loc.buffer if sub_loc else None
                    buf_kind = self.source_manager.getBufferKind(buf) if (self.source_manager and buf) else None
                    is_included = (buf_kind == pyslang.BufferKind.IncludeFile)
                    inc_raw_name = Path(self.source_manager.getRawFileName(buf)).name if (is_included and self.source_manager and buf) else None

                    self.all_subroutines[name] = {
                        "name": name,
                        "symbol": member,
                        "return_type": ret_type,
                        "args": args,
                        "is_mapped": name in self.task_mappings,
                        "mapping_target": self.task_mappings.get(name),
                        "origin_file": origin_file,
                        "origin_line": origin_line,
                        "is_included": is_included,
                        "include_name": inc_raw_name
                    }
                    if is_included and inc_raw_name in self.include_files:
                        self.include_files[inc_raw_name]["subroutines"].append(name)

            # 5. Discover initial processes
            for member in scope:
                if str(member.kind) == "SymbolKind.ProceduralBlock":
                    if member.procedureKind == pyslang.ast.ProceduralBlockKind.Initial:
                        line = self.compilation.sourceManager.getLineNumber(member.location)
                        file_name = self.compilation.sourceManager.getFileName(member.location)
                        name = member.name if hasattr(member, "name") and member.name else ""
                        if not name and member.syntax and hasattr(member.syntax, "statement"):
                            stmt = member.syntax.statement
                            if hasattr(stmt, "blockName") and stmt.blockName and hasattr(stmt.blockName, "name"):
                                name = str(stmt.blockName.name.value)
                        self.initial_processes.append({
                            "symbol": member,
                            "line": line,
                            "file": file_name,
                            "name": name,
                            "index": len(self.initial_processes)
                        })

    def discover_include_files(self):
        self.include_files = {}
        self.macro_definition_order = {}
        if not self.source_manager:
            return

        for buf_idx, b in enumerate(self.source_manager.getAllBuffers()):
            b_id = b.id if hasattr(b, "id") else buf_idx
            raw_text = str(self.source_manager.getSourceText(b)).rstrip("\x00\r\n ")
            for m in re.finditer(r"(?m)^\s*`define\s+([a-zA-Z_]\w*)", raw_text):
                self.macro_definition_order.setdefault(m.group(1), (b_id, m.start()))

            if self.source_manager.getBufferKind(b) == pyslang.BufferKind.IncludeFile:
                raw_fn = self.source_manager.getRawFileName(b)
                if not raw_fn:
                    continue
                c_header_name = re.sub(r"\.v[h]?$", ".h", Path(raw_fn).name)
                self.include_files[Path(raw_fn).name] = {
                    "raw_path": raw_fn,
                    "c_name": c_header_name,
                    "raw_text": raw_text,
                    "structs": {},
                    "parameters": {},
                    "variables": {},
                    "subroutines": [],
                    "content": ""
                }

        file_include_contexts = {}
        if hasattr(self, "compilation") and self.compilation:
            for t in self.compilation.getSyntaxTrees():
                proc_ranges = []
                def find_procs(node):
                    if node is None:
                        return
                    k = node.kind
                    if str(k) in (
                        "SyntaxKind.ProceduralBlock",
                        "SyntaxKind.InitialBlock",
                        "SyntaxKind.AlwaysBlock",
                        "SyntaxKind.TaskDeclaration",
                        "SyntaxKind.FunctionDeclaration"
                    ):
                        if hasattr(node, "sourceRange") and node.sourceRange:
                            proc_ranges.append((node.sourceRange, str(k)))
                    if hasattr(node, "members"):
                        for m in node.members:
                            find_procs(m)

                find_procs(t.root)

                for inc in t.getIncludeDirectives():
                    inc_loc = inc.syntax.sourceRange.start
                    inc_name = Path(inc.path).name
                    is_proc = False
                    for prange, pkind in proc_ranges:
                        if prange.start.buffer == inc_loc.buffer and prange.start.offset <= inc_loc.offset <= prange.end.offset:
                            fn = self.source_manager.getFileName(inc_loc)
                            line = self.source_manager.getLineNumber(inc_loc)
                            clean_kind = pkind.replace("SyntaxKind.", "")
                            self.unconvertible_warnings.append(
                                f"Include directive `include \"{inc.path}\" ({fn}:{line}) is located inside a procedural context ({clean_kind}). "
                                f"Restoring as file-scope C++ #include."
                            )
                            is_proc = True
                            break
                    file_include_contexts.setdefault(inc_name, []).append(is_proc)

        for inc_name, contexts in file_include_contexts.items():
            if inc_name in self.include_files:
                self.include_files[inc_name]["is_procedural_only"] = all(contexts)

    def transpile_subroutine(self, sub: str, is_static_inline: bool = False) -> list:
        info = self.all_subroutines[sub]
        arg_str = ", ".join([f"{a['c_type']} {a['name']}" for a in info["args"]])
        out_args = {a["name"] for a in info["args"] if "Out" in a["direction"] or "InOut" in a["direction"]}

        stmt_transpiler = CppStatementTranspiler(
            self.task_mappings,
            self.var_mappings,
            out_params=out_args,
            warnings=self.unconvertible_warnings,
            source_manager=self.source_manager,
            visited_comments=self.visited_comment_keys,
            discovered_macros=self.discovered_macros,
            cpp_std=self.cpp_std
        )

        lines = []
        sub_sym = info["symbol"]
        sub_comments = stmt_transpiler.extract_leading_comments(getattr(sub_sym, "syntax", None))
        for comm in sub_comments:
            for c_line in comm.splitlines():
                c_clean = c_line.strip()
                if c_clean:
                    lines.append(c_clean)

        origin_str = f"{info['origin_file']}:{info['origin_line']}"
        inc_note = f" [from `include \"{info['include_name']}\"]" if info.get("is_included") else ""
        lines.append(f"/* Auto-transpiled from SystemVerilog task: {sub}")
        lines.append(f" * Origin: {origin_str}{inc_note} */")
        qualifier = "inline " if is_static_inline else ""
        lines.append(f"{qualifier}{info['return_type']} {sub}({arg_str or 'void'}) {{")

        body_stmt = info["symbol"].body
        body_lines = stmt_transpiler.transpile_body(body_stmt, indent_level=1)
        lines.extend(body_lines)

        lines.append("}\n")
        return lines

    def _detect_include_guard(self, lines):
        """
        Detect standard include guard:
          `ifndef GUARD_MACRO
          `define GUARD_MACRO
          ...
          `endif
        Returns (c_guard_name, ifndef_line_idx, define_line_idx, endif_line_idx).
        If not found, returns (None, -1, -1, -1).
        """
        first_directive_idx = -1
        guard_macro = None
        for idx, line in enumerate(lines):
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("//") or trimmed.startswith("/*"):
                continue
            if trimmed.startswith("`ifndef"):
                m = re.match(r"^`ifndef\s+([a-zA-Z_]\w*)", trimmed)
                if m:
                    guard_macro = m.group(1)
                    first_directive_idx = idx
            break

        if first_directive_idx == -1 or not guard_macro:
            return None, -1, -1, -1

        second_directive_idx = -1
        for idx in range(first_directive_idx + 1, len(lines)):
            trimmed = lines[idx].strip()
            if not trimmed or trimmed.startswith("//") or trimmed.startswith("/*"):
                continue
            if trimmed.startswith("`define"):
                m = re.match(r"^`define\s+([a-zA-Z_]\w*)", trimmed)
                if m and m.group(1) == guard_macro:
                    second_directive_idx = idx
            break

        if second_directive_idx == -1:
            return None, -1, -1, -1

        depth = 0
        matching_endif_idx = -1
        for idx in range(first_directive_idx, len(lines)):
            trimmed = lines[idx].strip()
            if re.match(r"^`(ifndef|ifdef)\b", trimmed):
                depth += 1
            elif re.match(r"^`endif\b", trimmed):
                depth -= 1
                if depth == 0:
                    matching_endif_idx = idx
                    break

        if matching_endif_idx == -1:
            return None, -1, -1, -1

        c_guard = re.sub(r"_V[H]?$", "_H", guard_macro)
        if not c_guard.endswith("_H"):
            c_guard += "_H"

        return c_guard, first_directive_idx, second_directive_idx, matching_endif_idx

    def _format_remaining_header_items(self, include_structs, include_subs, include_params, include_vars, emitted_structs, emitted_subs, emitted_params):
        rem_lines = []
        rem_p = {k: v for k, v in include_params.items() if k not in emitted_params}
        if rem_p:
            rem_lines.append("\n/* --- Restored Parameters --- */")
            for pname, pval in rem_p.items():
                rem_lines.append(f"#define {pname:20s} {pval}")

        rem_s = {k: v for k, v in include_structs.items() if k not in emitted_structs}
        if rem_s:
            rem_lines.append("\n/* --- Restored Type Definitions --- */")
            for struct_name, fields in rem_s.items():
                rem_lines.append(f"struct {struct_name} {{")
                for f in fields:
                    dim_str = f"[{f['dim']}]" if f.get("dim") else ""
                    rem_lines.append(f"    {f['type']} {f['name']}{dim_str};")
                rem_lines.append("};\n")

        rem_subs = [s for s in include_subs if s not in emitted_subs]
        rem_subs.sort(key=self._get_sub_location_key)
        if rem_subs:
            rem_lines.append("\n/* --- Restored Subroutines --- */")
            for sub_name in rem_subs:
                lines = self.transpile_subroutine(sub_name, is_static_inline=True)
                rem_lines.extend(lines)

        return rem_lines

    def convert_vh_to_h(self, vh_content: str, include_subs: list = None, include_structs: dict = None, include_params: dict = None, include_vars: dict = None) -> str:
        include_subs = include_subs or []
        include_structs = include_structs or {}
        include_params = include_params or {}
        include_vars = include_vars or {}

        def normalize_radix(text: str) -> str:
            def repl(m):
                base = m.group(1).lower()
                digits = m.group(2).replace("_", "")
                if base == "h": return "0x" + digits
                if base == "b": return str(int(digits, 2))
                if base == "o": return "0" + digits
                if base == "d": return digits
                return m.group(0)
            return re.sub(r"(?:\b\d+)?\'[sS]?([hHbBdDoO])([0-9a-fA-F_]+)\b", repl, text)

        lines = vh_content.splitlines()
        c_guard, ifndef_idx, define_idx, endif_idx = self._detect_include_guard(lines)

        emitted_structs = set()
        emitted_subs = set()
        emitted_params = set()
        seen_macros = set()

        out_lines = []
        if c_guard:
            out_lines.append(f"#ifndef {c_guard}")
            out_lines.append(f"#define {c_guard}")
            out_lines.append("")

        i = 0
        while i < len(lines):
            line = lines[i]
            trimmed = line.strip()

            # Skip original include guard ifndef/define
            if i in (ifndef_idx, define_idx):
                i += 1
                continue

            # Matching endif of include guard
            if i == endif_idx:
                rem = self._format_remaining_header_items(
                    include_structs, include_subs, include_params, include_vars,
                    emitted_structs, emitted_subs, emitted_params
                )
                if rem:
                    out_lines.extend(rem)
                if c_guard:
                    out_lines.append(f"#endif /* {c_guard} */")
                i += 1
                continue

            # 1. Struct in-place replacement
            if re.match(r"^typedef\s+struct\b", trimmed):
                while i < len(lines) and not re.search(r"\}\s*(\w+)\s*;", lines[i]):
                    i += 1
                s_match = re.search(r"\}\s*(\w+)\s*;", lines[i]) if i < len(lines) else None
                s_name = s_match.group(1) if s_match else None
                if s_name and s_name in include_structs:
                    fields = include_structs[s_name]
                    out_lines.append(f"struct {s_name} {{")
                    for f in fields:
                        dim_str = f"[{f['dim']}]" if f.get("dim") else ""
                        out_lines.append(f"    {f['type']} {f['name']}{dim_str};")
                    out_lines.append("};\n")
                    emitted_structs.add(s_name)
                i += 1
                continue

            # 2. Task/Function in-place replacement
            if re.match(r"^(task|function)\b", trimmed):
                m_sub = re.match(r"^(?:task|function)\b[^(;]*?\b([a-zA-Z_]\w*)\s*(?:[\(;]|$)", trimmed)
                sub_name = m_sub.group(1) if m_sub else None
                while i < len(lines) and not re.match(r"^(endtask|endfunction)\b", lines[i].strip()):
                    i += 1
                if sub_name and sub_name in include_subs:
                    lines_c = self.transpile_subroutine(sub_name, is_static_inline=True)
                    out_lines.extend(lines_c)
                    emitted_subs.add(sub_name)
                i += 1
                continue

            # 3. Parameter / localparam in-place replacement
            m_param = re.match(r"^(?:parameter|localparam)\b[^=;]*?\b([a-zA-Z_]\w*)\s*=\s*([^;]+);", trimmed)
            if m_param:
                p_name = m_param.group(1)
                p_val = m_param.group(2).strip()
                val_c = include_params.get(p_name, normalize_radix(p_val))
                out_lines.append(f"#define {p_name:20s} {val_c}")
                emitted_params.add(p_name)
                seen_macros.add(p_name)
                i += 1
                continue

            # 4. Directive conversions (` -> #)
            if trimmed.startswith("`"):
                if re.match(r"^`include\b", trimmed):
                    inc_line = re.sub(r"^(\s*)`include\b", r"\1#include", line)
                    inc_line = re.sub(r'\.v[h]?([">])', r'.h\1', inc_line)
                    out_lines.append(inc_line)
                    i += 1
                    continue

                m_def = re.match(r"^`define\s+([a-zA-Z_]\w*)(.*)", trimmed)
                if m_def:
                    d_name, d_rest = m_def.group(1), m_def.group(2)
                    if d_name in seen_macros:
                        i += 1
                        continue
                    seen_macros.add(d_name)
                    if d_name in self.discovered_macros:
                        val_c = self.discovered_macros[d_name]
                        out_lines.append(f"#define {d_name} {val_c}".rstrip())
                    else:
                        norm_rest = normalize_radix(d_rest)
                        out_lines.append(f"#define {d_name}{norm_rest}".rstrip())
                    i += 1
                    continue

                # Skip conditional directives inside header: transpile-time choice is already baked into C++
                if re.match(r"^`(ifdef|ifndef|else|elsif|endif)\b", trimmed):
                    i += 1
                    continue

            # 5. Passthrough other content (comments, blank lines)
            out_lines.append(line)
            i += 1

        if not c_guard:
            auto_guard = "GENERATED_HEADER_H"
            wrapped = [f"#ifndef {auto_guard}", f"#define {auto_guard}", ""]
            wrapped.extend(out_lines)
            rem = self._format_remaining_header_items(
                include_structs, include_subs, include_params, include_vars,
                emitted_structs, emitted_subs, emitted_params
            )
            if rem:
                wrapped.extend(rem)
            wrapped.append(f"#endif /* {auto_guard} */")
            out_lines = wrapped
        else:
            rem = self._format_remaining_header_items(
                include_structs, include_subs, include_params, include_vars,
                emitted_structs, emitted_subs, emitted_params
            )
            if rem and not any(f"#endif /* {c_guard} */" in l for l in out_lines):
                out_lines.extend(rem)
                out_lines.append(f"#endif /* {c_guard} */")

        return "\n".join(out_lines) + "\n"

    def select_process(self, target_spec):
        if not self.initial_processes:
            raise ValueError("No 'initial' processes found in design.")

        if target_spec is None:
            self.selected_process = self.initial_processes[0]
            return

        target_str = str(target_spec).strip()

        for p in self.initial_processes:
            if target_str == f"{p['file']}:{p['line']}":
                self.selected_process = p
                return

        for p in self.initial_processes:
            if p["name"] and target_str == p["name"]:
                self.selected_process = p
                return

        clean_target = target_str
        if clean_target.startswith("@"):
            clean_target = clean_target[1:].strip()
        elif clean_target.lower().startswith("line:"):
            clean_target = clean_target[5:].strip()

        for p in self.initial_processes:
            if clean_target == str(p["line"]):
                self.selected_process = p
                return

        if target_str.isdigit() and int(target_str) < len(self.initial_processes):
            self.selected_process = self.initial_processes[int(target_str)]
            return

        self.list_processes()
        raise ValueError(f"Process matching '{target_spec}' not found.")

    def list_processes(self):
        print("\nAvailable 'initial' processes in design:")
        for p in self.initial_processes:
            name_str = f" ({p['name']})" if p['name'] else ""
            print(f"  [{p['index']}] {p['file']}:{p['line']}{name_str}")

    def run_reachability_analysis(self):
        if not self.selected_process:
            return

        file_op_names = {
            "$fopen", "$fread", "$fgets", "$fscanf", "$fseek", "$ftell",
            "$feof", "$rewind", "$fgetc", "$ungetc", "$ferror", "$fflush",
            "$readmemh", "$readmemb", "$fwrite", "$fdisplay", "$fclose"
        }

        def check_type_ref(t):
            curr = t
            while curr:
                t_name = getattr(curr, "name", "")
                if t_name in self.struct_definitions:
                    self.referenced_structs.add(t_name)
                curr = getattr(curr, "elementType", None)

        def visit_ast_node(node):
            node_type = type(node).__name__
            if hasattr(node, "type") and node.type:
                check_type_ref(node.type)
            if node_type == "CallExpression":
                sub = getattr(node, "subroutine", None)
                if hasattr(sub, "name"):
                    t_name = sub.name
                    if t_name in self.all_subroutines:
                        explore_subroutine(t_name)
                elif hasattr(sub, "subroutine"):
                    sys_name = sub.subroutine.name
                    self.reachable_sys_calls.add(sys_name)
                    if sys_name in file_op_names:
                        self.detected_file_ops.add(sys_name)
            elif node_type == "NamedValueExpression":
                v_name = node.symbol.name
                if v_name in self.module_variables:
                    self.referenced_module_vars.add(v_name)
                    v_sym = self.module_variables[v_name]
                    if hasattr(v_sym, "type") and v_sym.type:
                        check_type_ref(v_sym.type)
                if v_name in self.module_parameters:
                    self.referenced_parameters.add(v_name)
            elif node_type == "VariableDeclStatement":
                sym = getattr(node, "symbol", None)
                if sym:
                    if hasattr(sym, "type") and sym.type:
                        check_type_ref(sym.type)
                    if getattr(sym, "initializer", None):
                        sym.initializer.visit(visit_ast_node)
            return pyslang.ast.VisitAction.Advance

        def explore_subroutine(sub_name):
            if sub_name in self.reachable_subs:
                return
            self.reachable_subs.add(sub_name)

            sub_info = self.all_subroutines.get(sub_name)
            if sub_info:
                sym = sub_info["symbol"]
                if hasattr(sym, "returnType") and sym.returnType:
                    check_type_ref(sym.returnType)
                for arg in getattr(sym, "arguments", []):
                    if hasattr(arg, "type") and arg.type:
                        check_type_ref(arg.type)

            if sub_name in self.task_mappings:
                self.mapped_leafs.add(sub_name)
                return

            self.needed_codegen.add(sub_name)
            if sub_info:
                sub_info["symbol"].visit(visit_ast_node)

        self.selected_process["symbol"].visit(visit_ast_node)

        # Statically expand file paths accessed by reachable subroutines and initial block
        path_expander = ASTPathExpander(workdir=self.workdir, all_subroutines=self.all_subroutines)
        if hasattr(self.selected_process["symbol"], "body"):
            path_expander.walk_statement(self.selected_process["symbol"].body)

        for sub_name in self.needed_codegen:
            if sub_name not in [sig[0] for sig in path_expander.visited_calls] and sub_name in path_expander.effectful_subs:
                sub_info = self.all_subroutines.get(sub_name)
                if sub_info and hasattr(sub_info["symbol"], "body"):
                    path_expander.walk_statement(sub_info["symbol"].body)

        self.discovered_output_paths = sorted(list(path_expander.output_paths))
        self.discovered_input_paths = sorted([p for p in path_expander.input_paths if p not in path_expander.output_paths])
        self.unresolved_dynamic_paths = sorted([p for p in path_expander.unresolved_dynamic_paths if p not in path_expander.output_paths])

    def get_active_include_files(self) -> dict:
        """
        Return the subset of self.include_files that are actually used by the transpiled target process
        (via macros, reachable subroutines, structs, parameters, module variables, or nested includes).
        """
        if not self.include_files:
            return {}

        directly_used = set()
        for inc_name, inc_info in self.include_files.items():
            if inc_info.get("is_procedural_only", False):
                continue

            # 1. Any reachable subroutine defined in this include file
            if any(sub in self.needed_codegen for sub in inc_info.get("subroutines", [])):
                directly_used.add(inc_name)
                continue

            # 2. Any referenced struct defined in this include file
            if any(s in self.referenced_structs for s in inc_info.get("structs", {}).keys()):
                directly_used.add(inc_name)
                continue

            # 3. Any referenced parameter defined in this include file
            if any(p in self.referenced_parameters for p in inc_info.get("parameters", {}).keys()):
                directly_used.add(inc_name)
                continue

            # 4. Any referenced module variable defined in this include file
            if any(v in self.referenced_module_vars for v in inc_info.get("variables", {}).keys()):
                directly_used.add(inc_name)
                continue

            # 5. Any macro used in transpiled code that is defined in this include file
            inc_macros = {
                m.group(1)
                for m in re.finditer(r"`define\s+([a-zA-Z_]\w*)", inc_info.get("raw_text", ""))
            }
            if any(m in self.discovered_macros for m in inc_macros):
                directly_used.add(inc_name)
                continue

        # Transitively include headers that are `included by any active header
        active_set = set(directly_used)
        queue = list(directly_used)
        while queue:
            curr = queue.pop(0)
            curr_info = self.include_files.get(curr, {})
            raw_text = curr_info.get("raw_text", "")
            for m in re.finditer(r'(?m)^\s*`include\s+["<]([^">]+)[">]', raw_text):
                child_name = Path(m.group(1)).name
                if child_name in self.include_files and child_name not in active_set:
                    if not self.include_files[child_name].get("is_procedural_only", False):
                        active_set.add(child_name)
                        queue.append(child_name)

        return {k: v for k, v in self.include_files.items() if k in active_set}

    def generate_report(self):
        if self._cached_cpp_code is None:
            self.generate_cpp_code()

        active_includes = self.get_active_include_files()

        print("=" * 70)
        print("  Generic SV2CPP Transpiler Plan & Analysis Report")
        print("=" * 70)
        p = self.selected_process
        label_str = f" : {p['name']}" if p['name'] else ""
        print(f"Target Process       : {p['file']}:{p['line']} (initial begin{label_str})")
        th_str = f" (Thresholds: A<={self.embed_threshold_a}B, B<={self.embed_threshold_b}B, C>{self.embed_threshold_b}B)" if (self.embed_mode or "auto").lower() == "auto" else ""
        print(f"Data Embedding Mode  : {self.embed_mode.upper()}{th_str}")
        print(f"C++ Standard Target  : {self.cpp_std.upper()}")
        print("-" * 70)
        if active_includes:
            print("[0] Restored Headers (Verilog `include -> C++ #include):")
            for raw_name, info in active_includes.items():
                print(f"  - `include \"{raw_name}\"  -->  #include \"{info['c_name']}\" ({info['raw_path']})")
            print("")
        if self.discovered_macros:
            print("[0.5] Active Macros Preserved & Evaluated via AST:")
            ordered_macros = sorted(self.discovered_macros.keys(), key=self._get_macro_location_key)
            for mname in ordered_macros:
                c_val = self.discovered_macros[mname]
                print(f"  - #define {mname:25s} {c_val}")
            print("")
        print("[1] Reachable Subroutines from Target Process:")
        ordered_reachable = sorted(self.reachable_subs, key=self._get_sub_location_key)
        for sub in ordered_reachable:
            info = self.all_subroutines[sub]
            arg_str = ", ".join([f"{a['c_type']} {a['name']}" for a in info["args"]])
            sig = f"{info['return_type']} {sub}({arg_str or 'void'})"
            origin_desc = f" ({info['origin_file']}:{info['origin_line']})"
            if info.get("is_included"):
                origin_desc += f" [from `include \"{info['include_name']}\"]"
            if sub in self.mapped_leafs:
                print(f"  - [MAP TO API]   {sig}  -->  {self.task_mappings[sub]}(){origin_desc}")
            elif info.get("is_included"):
                c_hdr = re.sub(r"\.v[h]?$", ".h", info['include_name'])
                print(f"  - [IN HEADER]    {sig}  -->  #include \"{c_hdr}\"{origin_desc}")
            else:
                print(f"  - [CODEGEN BODY] {sig}{origin_desc}")

        print("\n[2] Pruned / Excluded Simulation-Only Subroutines:")
        excluded = [ex for ex in self.all_subroutines.keys() if ex not in self.reachable_subs]
        for ex in excluded:
            print(f"  - [EXCLUDED]     {ex}() (Unreachable / Simulation Harness)")

        if self.struct_definitions:
            print("\n[3] Auto-Discovered Struct Definitions:")
            for s_name, s_val in self.struct_definitions.items():
                fields = s_val["fields"] if isinstance(s_val, dict) else s_val
                origin_note = ""
                if isinstance(s_val, dict) and s_val.get("is_included"):
                    c_hdr = re.sub(r"\.v[h]?$", ".h", s_val["include_name"])
                    origin_note = f" [in #include \"{c_hdr}\"]"
                f_list = ", ".join([f"{f['type']} {f['name']}" for f in fields[:4]])
                if len(fields) > 4: f_list += ", ..."
                print(f"  - struct {s_name} {{ {f_list} }};{origin_note}")

        if self.module_parameters:
            print("\n[4] Auto-Discovered Module Parameters:")
            for name, p_val in self.module_parameters.items():
                val = p_val["val"] if isinstance(p_val, dict) else p_val
                origin_note = ""
                if isinstance(p_val, dict) and p_val.get("is_included"):
                    c_hdr = re.sub(r"\.v[h]?$", ".h", p_val["include_name"])
                    origin_note = f" [in #include \"{c_hdr}\"]"
                print(f"  - #define {name:20s} {val}{origin_note}")

        print("\n[5] Referenced Module Variables (Auto-Declared in C++):")
        ref_vars = list(self.referenced_module_vars)
        ref_vars.sort(key=self._get_var_location_key)
        for v in ref_vars:
            mapped = self.var_mappings.get(v)
            if mapped:
                print(f"  - {v:20s} -> Mapped to: {mapped}")
            else:
                print(f"  - {v:20s} -> Declared as static C++ variable")

        print("\n[6] Asset File Path Discovery & Dynamic Path Audit:")
        if self.discovered_input_paths:
            for path in self.discovered_input_paths:
                print(f"  - STATIC INPUT  : {path}")
        if self.discovered_output_paths:
            for path in self.discovered_output_paths:
                print(f"  - STATIC OUTPUT : {path}")

        if self.unresolved_dynamic_paths:
            print("\n  [WARNING] Dynamic file paths depending on runtime variable(s) detected:")
            for p in self.unresolved_dynamic_paths:
                print(f"    * {p}")
            print("  ==> These paths cannot be resolved statically at transpile-time.")
            print("  ==> For bare-metal execution, manually configure assets in g_user_assets")
            print("      and load data to designated memory addresses.")

        print("\n[7] Reachable System Calls & File Ops:")
        print(f"  System Calls : {sorted(list(self.reachable_sys_calls))}")
        print(f"  File Ops     : {sorted(list(self.detected_file_ops))}")

        if self.unconvertible_warnings:
            print("\n[8] Simulation-Only / Unconvertible Constructs Audit:")
            for w in self.unconvertible_warnings:
                print(f"  - [WARNING] {w}")
        print("=" * 70)

    def generate_cpp_code(self):
        """Generate complete, compilable bare-metal C++ code."""
        if self._cached_cpp_code is not None:
            return self._cached_cpp_code

        ordered_needed_subs = sorted(self.needed_codegen, key=self._get_sub_location_key)
        main_subs = [sub for sub in ordered_needed_subs if not self.all_subroutines[sub].get("is_included")]

        func_lines = []
        if ordered_needed_subs:
            func_lines.append("/* --- [5] Forward Declarations of Auto-Generated Functions --- */")
            for sub in ordered_needed_subs:
                info = self.all_subroutines[sub]
                arg_str = ", ".join([f"{a['c_type']} {a['name']}" for a in info["args"]])
                func_lines.append(f"{info['return_type']} {sub}({arg_str or 'void'});")
            func_lines.append("")

            for sub in main_subs:
                sub_code = self.transpile_subroutine(sub, is_static_inline=False)
                func_lines.extend(sub_code)

        # Entry Point (Transpiled from initial begin...end)
        main_lines = []
        main_lines.append("/* --- [7] Entry Point (Transpiled from initial begin...end) --- */")

        init_transpiler = CppStatementTranspiler(
            self.task_mappings,
            self.var_mappings,
            warnings=self.unconvertible_warnings,
            source_manager=self.source_manager,
            visited_comments=self.visited_comment_keys,
            discovered_macros=self.discovered_macros,
            cpp_std=self.cpp_std
        )

        init_sym = self.selected_process["symbol"]
        init_comments = init_transpiler.extract_leading_comments(getattr(init_sym, "syntax", None))
        for comm in init_comments:
            for c_line in comm.splitlines():
                c_clean = c_line.strip()
                if c_clean:
                    main_lines.append(c_clean)

        main_lines.append("int main(int argc, char **argv) {")
        main_lines.append("    sv_init_plusargs(argc, argv);")
        main_lines.append('    printf("[sv2cpp] Starting Bare-metal C++ Test Execution...\\n");')

        init_body_lines = init_transpiler.transpile_body(self.selected_process["symbol"].body, indent_level=1)
        main_lines.extend(init_body_lines)
        main_lines.append('    printf("[sv2cpp] Test Execution Finished.\\n");')
        main_lines.append("    return 0;")
        main_lines.append("}")

        lines = []
        lines.append("/*")
        lines.append(" * Automatically generated by generic sv2cpp transpiler")
        p = self.selected_process
        lines.append(f" * Source Process: {p['file']}:{p['line']}")
        lines.append(f" * Target: {self.cpp_std.upper()}")
        lines.append(f" * Data Embedding Mode: {self.embed_mode.upper()}")
        if self.unconvertible_warnings:
            lines.append(" *")
            lines.append(" * [WARNINGS] Unconvertible simulation constructs were detected and commented out:")
            for w in self.unconvertible_warnings:
                clean_w = str(w).replace("/*", "/[*]").replace("*/", "[*]/")
                lines.append(f" *   - {clean_w}")
        lines.append(" */\n")
        lines.append('#include "sv_runtime.hpp"')

        # Include restored headers corresponding to active Verilog `include files
        active_includes = self.get_active_include_files()
        file_scope_includes = list(active_includes.values())
        if file_scope_includes:
            lines.append("")
            lines.append("/* Restored Headers corresponding to Verilog `include */")
            for inc_info in file_scope_includes:
                lines.append(f'#include "{inc_info["c_name"]}"')

        # Macro Definitions (Preserving original source definition order)
        included_macros = set()
        for inc_info in active_includes.values():
            for m in re.finditer(r"`define\s+([a-zA-Z_]\w*)", inc_info.get("raw_text", "")):
                included_macros.add(m.group(1))

        main_macro_names = [
            m for m in self.discovered_macros.keys()
            if m not in included_macros
        ]
        main_macro_names.sort(key=self._get_macro_location_key)
        if main_macro_names:
            lines.append("/* --- [0.5] Macro Definitions (Auto-Extracted from `define) --- */")
            for mname in main_macro_names:
                lines.append(f"#define {mname} {self.discovered_macros[mname]}")
            lines.append("")

        # Struct Definitions (Main file only, preserving original source definition order)
        main_structs = {
            s: (v["fields"] if isinstance(v, dict) else v)
            for s, v in self.struct_definitions.items()
            if not (isinstance(v, dict) and v.get("is_included"))
        }
        if main_structs:
            lines.append("/* --- [0] Type Definitions (Auto-Extracted from SV typedef struct) --- */")
            for struct_name, fields in main_structs.items():
                lines.append(f"struct {struct_name} {{")
                for f in fields:
                    dim_str = f"[{f['dim']}]" if f['dim'] else ""
                    lines.append(f"    {f['type']} {f['name']}{dim_str};")
                lines.append("};\n")

        # Target Hardware C API Prototypes (Mapped Tasks)
        if self.task_mappings or self.extra_declarations:
            lines.append("/* --- [1] Target Hardware Library API Prototypes --- */")
            lines.append('#ifdef __cplusplus')
            lines.append('extern "C" {')
            lines.append('#endif')
            declared_apis = set()
            for sub, c_func in self.task_mappings.items():
                if any(re.search(rf"\b{c_func}\s*\(", decl.split("{")[0]) for decl in self.extra_declarations):
                    continue
                if c_func in declared_apis:
                    continue
                declared_apis.add(c_func)
                info = self.all_subroutines.get(sub)
                if info:
                    c_args = []
                    for a in info["args"]:
                        base_t = a["c_type"].replace("&", "").strip()
                        if ("Out" in a["direction"] or "Ref" in a["direction"]) and "[" not in a["name"]:
                            c_args.append(f"{base_t} *{a['name']}")
                        else:
                            c_args.append(f"{base_t} {a['name']}")
                    arg_str = ", ".join(c_args)
                    lines.append(f"extern {info['return_type']} {c_func}({arg_str or 'void'});")
                else:
                    lines.append(f"extern void {c_func}();")
            lines.append('#ifdef __cplusplus')
            lines.append('}')
            lines.append('#endif')

            for decl in self.extra_declarations:
                lines.append(decl)
            lines.append("")

        # Module Parameters & Localparams (Preserving original source definition order)
        main_params = {
            p: (v["val"] if isinstance(v, dict) else v)
            for p, v in self.module_parameters.items()
            if not (isinstance(v, dict) and v.get("is_included"))
        }
        if main_params:
            lines.append("/* --- [2] Module Parameters & Localparams (Auto-Extracted) --- */")
            for name, val in main_params.items():
                lines.append(f"#define {name} {val}")
            lines.append("")

        # Module Variables
        stmt_transpiler_helper = CppStatementTranspiler(
            source_manager=self.source_manager,
            discovered_macros=self.discovered_macros,
            cpp_std=self.cpp_std
        )
        unmapped_vars = [v for v in self.referenced_module_vars if v not in self.var_mappings]
        unmapped_vars.sort(key=self._get_var_location_key)
        if unmapped_vars:
            lines.append("/* --- [3] Module Global Variables (Auto-Extracted) --- */")
            for v_name in unmapped_vars:
                v_sym = self.module_variables[v_name]
                cpp_type, arr_dim = stmt_transpiler_helper.transpile_type(v_sym.type)
                is_struct = getattr(v_sym.type, "isStruct", False) or getattr(v_sym.type, "isAlias", False)
                is_string = getattr(v_sym.type, "isString", False)
                is_vector = "std::vector" in cpp_type
                init_expr = getattr(v_sym, "initializer", None)

                if init_expr:
                    stmt_transpiler_helper.expr_transpiler.has_unsupported = False
                    init_val = stmt_transpiler_helper.expr_transpiler.transpile(init_expr)
                    if stmt_transpiler_helper.expr_transpiler.has_unsupported:
                        init_str = " = {}" if (arr_dim or is_struct or is_vector) else ("" if is_string else " = 0")
                    else:
                        init_str = f" = {init_val}"
                elif is_string or is_vector:
                    init_str = ""
                elif arr_dim or is_struct:
                    init_str = " = {}"
                else:
                    init_str = " = 0"

                if arr_dim:
                    dim_suffix = arr_dim if arr_dim.startswith("[") else f"[{arr_dim}]"
                    lines.append(f"static {cpp_type} {v_name}{dim_suffix}{init_str};")
                else:
                    lines.append(f"static {cpp_type} {v_name}{init_str};")
            lines.append("")

        # Memory File API and Assets
        if self.discovered_input_paths or self.unresolved_dynamic_paths or self.discovered_output_paths:
            lines.append("/* --- [4] Bare-metal Memory File API & Asset Configuration --- */")

            if self.unresolved_dynamic_paths:
                lines.append("/*")
                lines.append(" * [WARNING] Dynamic file paths detected during transpilation!")
                lines.append(" * The following file paths depend on runtime variables (e.g. plusargs):")
                for p in sorted(self.unresolved_dynamic_paths):
                    lines.append(f" *   - {p}")
                lines.append(" *")
                lines.append(" * For bare-metal execution without a filesystem, please manually load data")
                lines.append(" * to designated memory addresses and register them in g_user_assets (or assign g_mem_assets).")
                lines.append(" */\n")

            if self.discovered_input_paths or self.discovered_output_paths:
                req_mode = (self.embed_mode or "auto").lower()
                lines.append(f"/* Embedded Asset Data (Mode: {self.embed_mode.upper()}) */")

                asset_table_entries = []

                if self.discovered_input_paths:
                    assets_info = []
                    current_offset_c = 0
                    has_mode_b = False
                    has_mode_c = False

                    for path in self.discovered_input_paths:
                        var_ident = "s_asset_" + re.sub(r"[^a-zA-Z0-9_]", "_", path)
                        disk_path = None
                        candidate = Path(self.workdir) / path
                        if not candidate.exists():
                            candidate = Path(path)
                        if candidate.exists() and candidate.is_file():
                            disk_path = candidate

                        file_size = disk_path.stat().st_size if (disk_path and disk_path.is_file()) else 0
                        fallback_size = 10000 if "input" in path else (1024 if "table" in path else 64)
                        eff_size = file_size if (disk_path and disk_path.is_file()) else fallback_size

                        if req_mode in ("a", "b", "c"):
                            asset_mode = req_mode
                        else:
                            if eff_size <= self.embed_threshold_a:
                                asset_mode = "a"
                            elif eff_size <= self.embed_threshold_b:
                                asset_mode = "b"
                            else:
                                asset_mode = "c"

                        if asset_mode == "b":
                            has_mode_b = True
                        elif asset_mode == "c":
                            has_mode_c = True

                        assets_info.append({
                            "path": path,
                            "var_ident": var_ident,
                            "disk_path": disk_path,
                            "file_size": file_size,
                            "eff_size": eff_size,
                            "mode": asset_mode
                        })

                    if has_mode_b:
                        lines.append("#if defined(__GNUC__) || defined(__clang__)")
                        lines.append("/* Macro for embedding binary asset into .rodata (Mode B) */")
                        lines.append("#define INCBIN_ASSET(sym, path) \\")
                        lines.append("    __asm__( \\")
                        lines.append('        ".section .rodata\\n" \\')
                        lines.append('        ".global " #sym "\\n" \\')
                        lines.append('        ".type " #sym ", @object\\n" \\')
                        lines.append('        ".align 4\\n" \\')
                        lines.append('        #sym ":\\n" \\')
                        lines.append('        ".incbin \\"" path "\\"\\n" \\')
                        lines.append("    ); \\")
                        lines.append("    extern const uint8_t sym[]\n")

                    if has_mode_c:
                        lines.append("/* Mode C: Fixed-Address / Memory-Mapped Asset Declarations */")
                        lines.append("#ifndef ASSET_ROM_BASE")
                        lines.append("#define ASSET_ROM_BASE 0x80000000ULL")
                        lines.append("#endif\n")

                    for info in assets_info:
                        path = info["path"]
                        var_ident = info["var_ident"]
                        disk_path = info["disk_path"]
                        file_size = info["file_size"]
                        eff_size = info["eff_size"]
                        m = info["mode"]

                        if m == "b":
                            if disk_path and disk_path.is_file():
                                abs_path = disk_path.resolve().as_posix()
                                lines.append(f'INCBIN_ASSET({var_ident}, "{abs_path}"); /* {path} ({file_size} bytes) */')
                                asset_table_entries.append((path, f"(uint8_t*){var_ident}", str(file_size)))
                            else:
                                lines.append(f"static const uint8_t {var_ident}[{eff_size}] = {{ 0 }};")
                                asset_table_entries.append((path, f"(uint8_t*){var_ident}", f"sizeof({var_ident})"))

                        elif m == "c":
                            current_offset_c = (current_offset_c + 3) & ~3
                            addr_macro = f"ASSET_ADDR_{var_ident}"
                            ptr_macro = f"ASSET_PTR_{var_ident}"
                            lines.append(f"#define {addr_macro} (ASSET_ROM_BASE + 0x{current_offset_c:08x}ULL) /* Size: {eff_size} bytes */")
                            lines.append(f"#define {ptr_macro} ((uint8_t*)(uintptr_t){addr_macro})\n")
                            asset_table_entries.append((path, ptr_macro, str(eff_size)))
                            current_offset_c += eff_size

                        else:  # Mode A
                            if disk_path and disk_path.is_file():
                                data = disk_path.read_bytes()
                                if len(data) == 0:
                                    lines.append(f"static const uint8_t {var_ident}[1] = {{ 0 }};")
                                else:
                                    lines.append(f"static const uint8_t {var_ident}[{len(data)}] = {{")
                                    for i in range(0, len(data), 16):
                                        chunk = data[i:i + 16]
                                        chunk_hex = ", ".join(f"0x{b:02x}" for b in chunk)
                                        if i + 16 < len(data):
                                            lines.append(f"    {chunk_hex},")
                                        else:
                                            lines.append(f"    {chunk_hex}")
                                    lines.append("};")
                                asset_table_entries.append((path, f"(uint8_t*){var_ident}", f"sizeof({var_ident})"))
                            else:
                                lines.append(f"static const uint8_t {var_ident}[{eff_size}] = {{ 0 }};")
                                asset_table_entries.append((path, f"(uint8_t*){var_ident}", f"sizeof({var_ident})"))

                    if has_mode_b:
                        lines.append("#endif /* Mode B GCC/Clang */")

                if self.discovered_output_paths:
                    lines.append("")
                    lines.append("/* Static Output Asset Buffers (Allocated statically in RAM) */")
                    for path in sorted(self.discovered_output_paths):
                        if any(e[0] == path for e in asset_table_entries):
                            continue
                        clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", path)
                        buf_ident = f"s_asset_buf_{clean_name}"
                        lines.append(f"static uint8_t {buf_ident}[MAX_MEM_WRITE_FILE_SIZE];")
                        asset_table_entries.append((path, buf_ident, f"sizeof({buf_ident})"))

                lines.append("")
                lines.append("/* Auto-Generated Static Asset Table (Unified Input & Output) */")
                lines.append("static MemAssetEntry g_auto_assets[] = {")
                for path, data_expr, size_expr in asset_table_entries:
                    lines.append(f'    {{ "{path}", {data_expr}, {size_expr} }},')
                lines.append("    { nullptr, nullptr, 0 }")
                lines.append("};\n")
                lines.append("/* Register active asset table */")
                lines.append("MemAssetEntry *g_mem_assets = g_auto_assets;\n")

            elif self.unresolved_dynamic_paths:
                lines.append("/* Manual Asset Table */")
                lines.append('extern "C" __attribute__((weak)) MemAssetEntry g_user_assets[] = {')
                lines.append("    { nullptr, nullptr, 0 }")
                lines.append("};\n")
                lines.append("/* Register active asset table */")
                lines.append("MemAssetEntry *g_mem_assets = g_user_assets;\n")

        lines.extend(func_lines)
        lines.extend(main_lines)

        self._cached_cpp_code = "\n".join(lines)
        return self._cached_cpp_code


def main():
    parser = argparse.ArgumentParser(
        description="sv2cpp - Generic SystemVerilog to Bare-metal / Embedded C++ Transpiler\n"
                    "Analyzes SystemVerilog testbenches / procedural tasks via slang AST and\n"
                    "transpiles to clean, standalone modern C++ for embedded systems, simulation, and emulation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 1. Transpile using EDA filelist (-f):
  python3 sv2cpp.py -f examples/tool_sim.f -c configs/jpeg_tool.yaml --process 0 -o build/test.cpp

  # 2. Transpile using direct EDA arguments (+incdir+, +define+, sources):
  python3 sv2cpp.py sim/jpeg_tool_tb.sv +incdir+rtl +define+TEST_MODE=1 -c configs/jpeg_tool.yaml -o build/test.cpp
"""
    )
    g_sv2cpp = parser.add_argument_group("sv2cpp Transpiler Options")
    g_sv2cpp.add_argument("-c", "--config", metavar="FILE",
                          help="YAML configuration file for task/variable mappings and peripheral API")
    g_sv2cpp.add_argument("--process", metavar="PROC", default=None,
                          help="Target 'initial' process by label name, line number, or index (default: first process)")
    g_sv2cpp.add_argument("--embed-mode", choices=["auto", "a", "b", "c"], default=None,
                          help="Data embedding mode (auto=size-based, a=c_array, b=incbin, c=fixed_addr)")
    g_sv2cpp.add_argument("--embed-threshold-a", type=int, default=None,
                          help="Threshold file size in bytes for Mode A in auto mode (default: 256)")
    g_sv2cpp.add_argument("--embed-threshold-b", type=int, default=None,
                          help="Threshold file size in bytes for Mode B in auto mode (default: 1024)")
    g_sv2cpp.add_argument("--cpp-std", default=None, choices=["c++11", "c++14", "c++17", "c++20"],
                          help="Target C++ standard (default: c++17)")
    g_sv2cpp.add_argument("--workdir", "--cwd", metavar="DIR", default=".",
                          help="Base working directory for resolving simulation file paths (default: .)")
    g_sv2cpp.add_argument("-o", "--output", metavar="FILE",
                          help="Output C++ file path (prints preview to stdout if omitted)")

    parser.add_argument_group(
        "EDA / Slang Options (Passed directly to compiler front-end)",
        "Standard EDA flags supported directly by slang:\n"
        "  -f <FILE>            Read command line options and file lists from file\n"
        "  -F <FILE>            Like -f, but relative paths are relative to the file\n"
        "  +incdir+<DIR>, -I    Add include directory for `include\n"
        "  +define+<M>[=<V>]    Define preprocessor macro\n"
        "  -D <M>[=<V>]         Define preprocessor macro\n"
        "  <sources.sv>         SystemVerilog source files"
    )

    args, slang_args = parser.parse_known_args()

    config = {}
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    for inc in config.get("include_dirs", []):
        slang_args.append(f"-I{inc}")
    for df in config.get("defines", []):
        slang_args.append(f"+define+{df}")

    if not slang_args:
        parser.error("No SystemVerilog sources or EDA arguments specified (e.g. -f filelist.f or sources.sv)")

    task_mappings = config.get("task_mappings", {})
    var_mappings = config.get("variable_mappings", {})
    extra_declarations = config.get("extra_declarations", [])
    embed_mode = args.embed_mode or config.get("embed_mode", "auto")
    embed_th_a = args.embed_threshold_a if args.embed_threshold_a is not None else config.get("embed_threshold_a", 256)
    embed_th_b = args.embed_threshold_b if args.embed_threshold_b is not None else config.get("embed_threshold_b", 1024)
    cpp_std = args.cpp_std or config.get("cpp_std", "c++17")

    analyzer = SV2CppAnalyzer(
        slang_args,
        task_mappings=task_mappings,
        var_mappings=var_mappings,
        embed_mode=embed_mode,
        workdir=args.workdir,
        extra_declarations=extra_declarations,
        embed_threshold_a=embed_th_a,
        embed_threshold_b=embed_th_b,
        cpp_std=cpp_std
    )
    analyzer.compile()
    analyzer.discover_elements()

    try:
        analyzer.select_process(args.process)
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    analyzer.run_reachability_analysis()
    analyzer.generate_report()

    cpp_code = analyzer.generate_cpp_code()
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(cpp_code, encoding="utf-8")
        print(f"\n[INFO] Generated C++ code written to: {args.output}")

        for inc_info in analyzer.get_active_include_files().values():
            h_path = out_path.parent / inc_info["c_name"]
            raw_inc_name = Path(inc_info["raw_path"]).name
            inc_subs = [
                s for s in analyzer.needed_codegen
                if analyzer.all_subroutines[s].get("include_name") == raw_inc_name
            ]
            inc_structs = inc_info.get("structs", {})
            inc_params = inc_info.get("parameters", {})
            inc_vars = inc_info.get("variables", {})
            fresh_content = analyzer.convert_vh_to_h(
                inc_info["raw_text"],
                include_subs=inc_subs,
                include_structs=inc_structs,
                include_params=inc_params,
                include_vars=inc_vars
            )
            h_path.write_text(fresh_content, encoding="utf-8")
            print(f"[INFO] Restored Header written to: {h_path}")
    else:
        print("\n" + "=" * 70)
        print("  Generated Bare-metal C++ Code Preview (Transpiled):")
        print("=" * 70)
        if len(cpp_code) > 4000:
            print(cpp_code[:3000] + "\n... [TRUNCATED FOR DISPLAY] ...\n" + cpp_code[-1000:])
        else:
            print(cpp_code)


if __name__ == "__main__":
    main()
