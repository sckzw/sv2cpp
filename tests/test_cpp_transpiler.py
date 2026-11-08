#!/usr/bin/env python3
"""
Unit and integration tests for sv2cpp (SystemVerilog to C++ Transpiler).
Verifies C++ code generation, reference passing, std::string, std::vector,
queue methods, structs, macros, and end-to-end execution with g++.
"""

import sys
import shutil
import subprocess
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
PYSLANG_ENV = ROOT_DIR / "pyslang_env"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(PYSLANG_ENV) not in sys.path:
    sys.path.insert(0, str(PYSLANG_ENV))

from sv2cpp import SV2CppAnalyzer


class SV2CppTestBase(unittest.TestCase):
    def init_workdir(self, suite_name: str) -> Path:
        workdir = ROOT_DIR / "build" / suite_name
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.runtime_dir = ROOT_DIR / "runtime"
        self.examples_dir = ROOT_DIR / "examples"
        return workdir

    def transpile_sv(self, sv_content: str, module_name: str, task_mappings: dict = None, var_mappings: dict = None, embed_mode: str = "auto", extra_declarations: list = None, cpp_std: str = "c++17", embed_threshold_a: int = 256, embed_threshold_b: int = 1024):
        top_path = self.workdir / f"{module_name}.sv"
        top_path.write_text(sv_content, encoding="utf-8")

        analyzer = SV2CppAnalyzer(
            [str(top_path), f"-I{self.workdir}"],
            task_mappings=task_mappings,
            var_mappings=var_mappings,
            embed_mode=embed_mode,
            workdir=str(self.workdir),
            extra_declarations=extra_declarations,
            embed_threshold_a=embed_threshold_a,
            embed_threshold_b=embed_threshold_b,
            cpp_std=cpp_std
        )
        analyzer.compile()
        analyzer.discover_elements()
        if analyzer.initial_processes:
            analyzer.select_process(0)
            analyzer.run_reachability_analysis()

        cpp_code = analyzer.generate_cpp_code()
        out_cpp = self.workdir / f"{module_name}.cpp"
        out_cpp.write_text(cpp_code, encoding="utf-8")

        for inc_name, inc_info in analyzer.include_files.items():
            if inc_info.get("is_procedural_only", False):
                continue
            h_name = inc_info["c_name"]
            out_h = self.workdir / h_name
            inc_subs = [s for s in analyzer.needed_codegen if analyzer.all_subroutines[s].get("include_name") == inc_name]
            h_content = analyzer.convert_vh_to_h(
                inc_info["raw_text"],
                include_subs=inc_subs,
                include_structs=inc_info.get("structs", {}),
                include_params=inc_info.get("parameters", {}),
                include_vars=inc_info.get("variables", {})
            )
            out_h.write_text(h_content, encoding="utf-8")

        return analyzer, out_cpp, cpp_code

    def compile_and_run(self, out_cpp: Path, bin_name: str, extra_files: list = None, run_args: list = None):
        bin_path = self.workdir / bin_name

        # Compile any C files separately with gcc to preserve C ABI symbol names
        c_objs = []
        c_srcs = []
        if extra_files:
            for ef in extra_files:
                p = Path(ef)
                if p.suffix == ".c":
                    c_srcs.append(p)

        for cs in c_srcs:
            obj_path = self.workdir / f"{cs.stem}.o"
            gcc_cmd = ["gcc", "-O2", "-std=c99", f"-I{self.runtime_dir}", f"-I{self.workdir}", "-c", str(cs), "-o", str(obj_path)]
            res = subprocess.run(gcc_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                self.fail(f"gcc compilation of {cs} failed:\n{res.stderr}")
            c_objs.append(str(obj_path))

        cpp_objs = []
        cpp_srcs = [self.runtime_dir / "sv_runtime.cpp"]
        if extra_files:
            for ef in extra_files:
                p = Path(ef)
                if p.suffix in (".cpp", ".cc"):
                    cpp_srcs.append(p)

        for cps in cpp_srcs:
            obj_path = self.workdir / f"{cps.stem}.o"
            gpp_cmd = ["g++", "-O2", "-std=c++17", f"-I{self.runtime_dir}", f"-I{self.workdir}", "-c", str(cps), "-o", str(obj_path)]
            res = subprocess.run(gpp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                self.fail(f"g++ compilation of {cps} failed:\n{res.stderr}")
            cpp_objs.append(str(obj_path))

        cmd = [
            "g++", "-O2", "-std=c++17", "-Wall",
            f"-I{self.runtime_dir}",
            f"-I{self.workdir}",
            str(out_cpp),
        ] + c_objs + cpp_objs + ["-o", str(bin_path)]

        compile_res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if compile_res.returncode != 0:
            self.fail(f"g++ compilation failed:\n{compile_res.stderr}\nCommand: {' '.join(cmd)}")

        run_cmd = [str(bin_path)] + (run_args or [])
        run_res = subprocess.run(run_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(self.workdir))
        return run_res.stdout, run_res.stderr, run_res.returncode


class TestSV2CppTranspiler(SV2CppTestBase):
    def setUp(self):
        self.init_workdir(f"unittest_cpp_{self._testMethodName}")

    def test_01_literals_and_operators(self):
        """Test arithmetic, bitwise, shift, ternary operators, and radix preservation in C++."""
        sv = """
module tb_ops;
    initial begin
        int a, b;
        int sum, diff, prod, quot, rem;
        int bw_and, bw_or, bw_xor;
        int shl, shr;
        int ternary_res;
        int hex_val;
        int pow_val, xnor_val, red_or, red_and;
        bit [7:0] byte_val;

        a = 40;
        b = 2;
        hex_val = 'hbeef;
        pow_val = 2 ** 5;
        xnor_val = 5 ~^ 3;
        byte_val = 8'hff;
        red_or = |a;
        red_and = &byte_val;

        sum = a + b;
        diff = a - b;
        prod = a * b;
        quot = a / b;
        rem = a % b;

        bw_and = a & b;
        bw_or = a | b;
        bw_xor = a ^ b;

        shl = a << 2;
        shr = a >> 1;

        ternary_res = (a > b) ? 100 : 200;

        if (a < 50 || b > 100) begin
            $display("CLEAN_IF_OK");
        end
        if ((a < 50) || (b > 100)) begin
            $display("PAREN_IF_OK");
        end

        $display("SUM: %0d", sum);
        $display("DIFF: %0d", diff);
        $display("PROD: %0d", prod);
        $display("QUOT: %0d", quot);
        $display("REM: %0d", rem);
        $display("AND: %0d", bw_and);
        $display("OR: %0d", bw_or);
        $display("XOR: %0d", bw_xor);
        $display("SHL: %0d", shl);
        $display("SHR: %0d", shr);
        $display("TERNARY: %0d", ternary_res);
        $display("HEX: %0x", hex_val);
        $display("POW: %0d", pow_val);
        $display("XNOR: %0d", xnor_val);
        $display("RED_OR: %0d", red_or);
        $display("RED_AND: %0d", red_and);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_ops")
        # Verify clean operator formatting without redundant parentheses
        self.assertIn("if (a < 50 || b > 100)", cpp_code)
        self.assertIn("if ((a < 50) || (b > 100))", cpp_code)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_ops")
        self.assertEqual(code, 0)
        self.assertIn("CLEAN_IF_OK", stdout)
        self.assertIn("PAREN_IF_OK", stdout)
        self.assertIn("SUM: 42", stdout)
        self.assertIn("DIFF: 38", stdout)
        self.assertIn("PROD: 80", stdout)
        self.assertIn("QUOT: 20", stdout)
        self.assertIn("REM: 0", stdout)
        self.assertIn("AND: 0", stdout)
        self.assertIn("OR: 42", stdout)
        self.assertIn("XOR: 42", stdout)
        self.assertIn("SHL: 160", stdout)
        self.assertIn("SHR: 20", stdout)
        self.assertIn("TERNARY: 100", stdout)
        self.assertIn("HEX: beef", stdout)
        self.assertIn("POW: 32", stdout)
        self.assertIn("XNOR: -7", stdout)
        self.assertIn("RED_OR: 1", stdout)
        self.assertIn("RED_AND: 1", stdout)

    def test_02_strings_and_sformat(self):
        """Test std::string transpilation, concatenation, $sformat and $sformatf."""
        sv = """
module tb_strings;
    initial begin
        string s1, s2, combined;
        string formatted;
        int count;

        s1 = "Hello";
        s2 = "C++";
        combined = {s1, ", ", s2, " World!"};
        $display("COMBINED: %s", combined);

        count = 123;
        $sformat(formatted, "Count is %0d", count);
        $display("SFORMAT: %s", formatted);

        $display("DIRECT_SFORMATF: %s", $sformatf("Value: 0x%04x", 255));
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_strings")
        self.assertIn("std::string", cpp_code)
        self.assertIn("sv_sformat(formatted,", cpp_code)
        self.assertIn("sv_sformatf(", cpp_code)
        stdout, stderr, code = self.compile_and_run(out_cpp, "run_strings")
        self.assertEqual(code, 0)
        self.assertIn("COMBINED: Hello, C++ World!", stdout)
        self.assertIn("SFORMAT: Count is 123", stdout)
        self.assertIn("DIRECT_SFORMATF: Value: 0x00ff", stdout)

    def test_03_queues_and_methods(self):
        """Test SV queues ([$]) transpiled to std::vector with push_back, pop_front, size, sort, reverse."""
        sv = """
module tb_queues;
    initial begin
        int q[$];
        int val;
        int sz;

        q.push_back(30);
        q.push_back(10);
        q.push_back(20);
        $display("SIZE: %0d", q.size());

        q.sort();
        $display("SORTED_0: %0d", q[0]);
        $display("SORTED_1: %0d", q[1]);
        $display("SORTED_2: %0d", q[2]);

        val = q.pop_front();
        $display("POPPED: %0d", val);
        $display("REMAINING: %0d", q.size());

        q.reverse();
        $display("REVERSED_0: %0d", q[0]);

        q.delete();
        $display("AFTER_DELETE: %0d", q.size());
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_queues")
        self.assertIn("std::vector<int32_t>", cpp_code)
        stdout, stderr, code = self.compile_and_run(out_cpp, "run_queues")
        self.assertEqual(code, 0)
        self.assertIn("SIZE: 3", stdout)
        self.assertIn("SORTED_0: 10", stdout)
        self.assertIn("SORTED_1: 20", stdout)
        self.assertIn("SORTED_2: 30", stdout)
        self.assertIn("POPPED: 10", stdout)
        self.assertIn("REMAINING: 2", stdout)
        self.assertIn("REVERSED_0: 30", stdout)
        self.assertIn("AFTER_DELETE: 0", stdout)

    def test_04_pass_by_reference_subroutines(self):
        """Test SV output/inout/ref arguments transpiled to natural C++ references (Type&)."""
        sv = """
module tb_ref;
    task compute(input int in_a, input int in_b, output int out_sum, inout int acc);
        out_sum = in_a + in_b;
        acc = acc + out_sum;
    endtask

    task mutate_string(input string prefix, inout string text);
        text = {prefix, text};
    endtask

    initial begin
        int sum_res;
        int total;
        string msg;

        total = 100;
        compute(10, 20, sum_res, total);
        $display("SUM: %0d, TOTAL: %0d", sum_res, total);

        msg = "World";
        mutate_string("Hello ", msg);
        $display("MUTATED: %s", msg);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_ref")
        # Ensure signatures use Type&
        self.assertIn("int32_t& out_sum", cpp_code)
        self.assertIn("int32_t& acc", cpp_code)
        self.assertIn("std::string& text", cpp_code)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_ref")
        self.assertEqual(code, 0)
        self.assertIn("SUM: 30, TOTAL: 130", stdout)
        self.assertIn("MUTATED: Hello World", stdout)

    def test_05_structs_and_assignment_patterns(self):
        """Test typedef struct definitions and struct field assignment."""
        sv = """
module tb_structs;
    typedef struct {
        int id;
        string tag;
        logic [15:0] value;
    } item_t;

    initial begin
        item_t it;
        it.id = 7;
        it.tag = "ITEM_ALPHA";
        it.value = 16'h1234;

        $display("ITEM: id=%0d tag=%s val=0x%04x", it.id, it.tag, it.value);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_structs")
        self.assertIn("struct item_t", cpp_code)
        stdout, stderr, code = self.compile_and_run(out_cpp, "run_structs")
        self.assertEqual(code, 0)
        self.assertIn("ITEM: id=7 tag=ITEM_ALPHA val=0x1234", stdout)

    def test_06_control_flow_and_loops(self):
        """Test if-else, case (switch), while, for, repeat, do-while loops in C++."""
        sv = """
module tb_control;
    initial begin
        int x;
        int repeat_cnt;
        int while_cnt;
        int for_sum;
        int case_res;

        x = 5;
        if (x > 3) begin
            $display("IF: PASS");
        end else begin
            $display("IF: FAIL");
        end

        case (x)
            1: case_res = 10;
            5: case_res = 50;
            default: case_res = 999;
        endcase
        $display("CASE: %0d", case_res);

        repeat_cnt = 0;
        repeat (4) begin
            repeat_cnt = repeat_cnt + 1;
        end
        $display("REPEAT: %0d", repeat_cnt);

        while_cnt = 0;
        while (while_cnt < 3) begin
            while_cnt = while_cnt + 1;
        end
        $display("WHILE: %0d", while_cnt);

        for_sum = 0;
        for (int i = 0; i < 5; i++) begin
            for_sum = for_sum + i;
        end
        $display("FOR: %0d", for_sum);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_control")
        stdout, stderr, code = self.compile_and_run(out_cpp, "run_control")
        self.assertEqual(code, 0)
        self.assertIn("IF: PASS", stdout)
        self.assertIn("CASE: 50", stdout)
        self.assertIn("REPEAT: 4", stdout)
        self.assertIn("WHILE: 3", stdout)
        self.assertIn("FOR: 10", stdout)

    def test_07_plusargs_and_hal_api_mapping(self):
        """Test task_mappings with C HAL API (dummy_hal.c) and plusargs integration."""
        sv = """
module tb_hal;
    task apb_wr(input int addr, input int data, input bit exp_err);
    endtask

    task apb_rd(input int addr, output int data, input bit exp_err);
    endtask

    initial begin
        int rd_data;
        string job_name;
        int job_id;

        apb_wr('h10, 'hcafe, 0);
        apb_rd(4, rd_data, 0); /* STATUS register in dummy_hal returns 1 or 2 */
        $display("STATUS_REG: %0d", rd_data);

        if ($value$plusargs("JOB=%s", job_name)) begin
            $display("JOB_ARG: %s", job_name);
        end

        if ($value$plusargs("ID=%d", job_id)) begin
            $display("ID_ARG: %0d", job_id);
        end
    end
endmodule
"""
        task_mappings = {
            "apb_wr": "WriteReg",
            "apb_rd": "ReadReg"
        }
        analyzer, out_cpp, cpp_code = self.transpile_sv(
            sv, "test_hal",
            task_mappings=task_mappings
        )
        dummy_hal = self.examples_dir / "dummy_hal.c"
        stdout, stderr, code = self.compile_and_run(
            out_cpp, "run_hal",
            extra_files=[dummy_hal],
            run_args=["+JOB=batch_proc", "+ID=42"]
        )
        self.assertEqual(code, 0)
        self.assertIn("STATUS_REG: 1", stdout)
        self.assertIn("JOB_ARG: batch_proc", stdout)
        self.assertIn("ID_ARG: 42", stdout)

    def test_08_file_io_and_mem_assets(self):
        """Test $fopen, $fscanf, $fgets, $rewind, $ferror, $readmemh with embedded asset table in C++."""
        self.init_workdir("unittest_cpp_file_io")
        data_file = self.workdir / "numbers.txt"
        data_file.write_text("100 200 300\n", encoding="utf-8")

        hex_file = self.workdir / "table.hex"
        hex_file.write_text("10 20 30 40\n", encoding="utf-8")

        sv = """
module tb_file_io;
    initial begin
        int fd;
        int n1, n2, n3;
        int hex_arr[4];
        string line;
        int chars_read;
        int err_code;
        string err_msg;

        fd = $fopen("numbers.txt", "r");
        if (fd != 0) begin
            chars_read = $fgets(line, fd);
            $display("FGETS_COUNT: %0d", chars_read);
            $display("FGETS_LINE: %s", line);
            $rewind(fd);
            $fscanf(fd, "%d %d %d", n1, n2, n3);
            $display("SCANNED: %0d %0d %0d", n1, n2, n3);
            err_code = $ferror(fd, err_msg);
            $display("FERROR_CODE: %0d", err_code);
            $fclose(fd);
        end

        $readmemh("table.hex", hex_arr);
        $display("HEX_TABLE: 0x%02x 0x%02x 0x%02x 0x%02x", hex_arr[0], hex_arr[1], hex_arr[2], hex_arr[3]);

        // Test $fdisplay / $fwrite and read-back via unified asset table
        fd = $fopen("generated.txt", "w");
        if (fd != 0) begin
            $fwrite(fd, "MAGIC: %0d\\n", 42);
            $fdisplay(fd, "DATA: %0d", 12345);
            $fclose(fd);
        end

        // Read back from common asset table
        fd = $fopen("generated.txt", "r");
        if (fd != 0) begin
            chars_read = $fgets(line, fd);
            $display("READBACK_LINE1: %s", line);
            chars_read = $fgets(line, fd);
            $display("READBACK_LINE2: %s", line);
            $fclose(fd);
        end
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_file_io")
        self.assertIn("mem_fgets(line, fd)", cpp_code)
        self.assertIn("mem_rewind(fd)", cpp_code)
        self.assertIn("mem_ferror(fd, err_msg)", cpp_code)
        self.assertIn("mem_fwrite(fd, \"MAGIC: %d\\n\", 42)", cpp_code)
        self.assertIn("mem_fdisplay(fd, \"DATA: %d\\n\", 12345)", cpp_code)
        stdout, stderr, code = self.compile_and_run(out_cpp, "run_file_io")
        self.assertEqual(code, 0)
        self.assertIn("FGETS_COUNT: 12", stdout)
        self.assertIn("FGETS_LINE: 100 200 300", stdout)
        self.assertIn("SCANNED: 100 200 300", stdout)
        self.assertIn("FERROR_CODE: 0", stdout)
        self.assertIn("HEX_TABLE: 0x10 0x20 0x30 0x40", stdout)
        self.assertIn("READBACK_LINE1: MAGIC: 42", stdout)
        self.assertIn("READBACK_LINE2: DATA: 12345", stdout)

    def test_09_include_restoration(self):
        """Test restoration of included header files (.vh -> .h / .hpp) in C++."""
        self.init_workdir("unittest_cpp_include")
        vh_file = self.workdir / "header.vh"
        vh_file.write_text("""
`ifndef HEADER_VH
`define HEADER_VH

`define MAGIC_CONST 32'hcafe_1234
localparam int BUFFER_SIZE = 1024;

`ifdef UNUSED_OPT
`define DUMMY_VAL 999
`endif

typedef struct {
    int id;
    int flags;
} packet_hdr_t;

task set_flags(output int f);
    f = 32'h55aa;
endtask

`endif // HEADER_VH

`ifdef SIM_DEBUG_TRAIL
`define TRAILING_VAL 123
`endif
""", encoding="utf-8")

        sv = """
`include "header.vh"

module tb_include;
    initial begin
        int val;
        int flg;
        packet_hdr_t pkt;

        val = `MAGIC_CONST;
        pkt.id = BUFFER_SIZE;
        set_flags(flg);

        $display("MAGIC: 0x%08x", val);
        $display("BUF_SIZE: %0d", pkt.id);
        $display("FLAGS: 0x%04x", flg);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_include")
        out_h = self.workdir / "header.h"
        self.assertTrue(out_h.exists())
        h_text = out_h.read_text(encoding="utf-8")

        # Verify include guard is clean and properly closed
        self.assertIn("#ifndef HEADER_H", h_text)
        self.assertIn("#define HEADER_H", h_text)
        self.assertIn("#endif /* HEADER_H */", h_text)

        # Verify struct and function are correctly inside include guard, NOT trailing after it
        endif_pos = h_text.find("#endif /* HEADER_H */")
        struct_pos = h_text.find("struct packet_hdr_t")
        func_pos = h_text.find("set_flags")
        self.assertGreater(struct_pos, 0)
        self.assertGreater(func_pos, 0)
        self.assertLess(struct_pos, endif_pos)
        self.assertLess(func_pos, endif_pos)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_include")
        self.assertEqual(code, 0)
        self.assertIn("MAGIC: 0xcafe1234", stdout)
        self.assertIn("BUF_SIZE: 1024", stdout)
        self.assertIn("FLAGS: 0x55aa", stdout)

    def test_10_multidimensional_arrays(self):
        """Test local multidimensional unpacked arrays and subroutine arguments."""
        sv = """
module tb_multi_arr;
    task process_grid(input logic [7:0] in_grid [3:0][3:0], output logic [15:0] out_edges [3:0][1:0]);
        logic [7:0] local_padded [5:0][5:0];
        int x, y;

        for (y = 0; y < 6; y++) begin
            for (x = 0; x < 6; x++) begin
                local_padded[y][x] = 8'h00;
            end
        end

        for (y = 0; y < 4; y++) begin
            for (x = 0; x < 4; x++) begin
                local_padded[y + 1][x + 1] = in_grid[y][x];
            end
        end

        for (y = 0; y < 4; y++) begin
            out_edges[y][0] = local_padded[y + 1][1];
            out_edges[y][1] = local_padded[y + 1][4];
        end
    endtask

    initial begin
        logic [7:0] grid [3:0][3:0];
        logic [15:0] edges [3:0][1:0];
        int r, c;

        for (r = 0; r < 4; r++) begin
            for (c = 0; c < 4; c++) begin
                grid[r][c] = (r * 10) + c;
            end
        end

        process_grid(grid, edges);

        $display("EDGE_0_0: %0d, EDGE_0_1: %0d", edges[0][0], edges[0][1]);
        $display("EDGE_3_0: %0d, EDGE_3_1: %0d", edges[3][0], edges[3][1]);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_multi_arr")
        self.assertIn("uint8_t in_grid[4][4]", cpp_code)
        self.assertIn("uint16_t out_edges[4][2]", cpp_code)
        self.assertIn("uint8_t local_padded[6][6]", cpp_code)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_multi_arr")
        self.assertEqual(code, 0)
        self.assertIn("EDGE_0_0: 0, EDGE_0_1: 3", stdout)
        self.assertIn("EDGE_3_0: 30, EDGE_3_1: 33", stdout)

    def test_11_eda_filelist_and_options(self):
        """Test EDA command-line options (-f, -F, +incdir+, +define+, multiple sources, preview)."""
        # 1. Test -f <filelist.f> with +incdir+, +define+, and comments (#, //, /* */)
        inc_dir = self.workdir / "inc_11"
        inc_dir.mkdir(parents=True, exist_ok=True)
        defs_vh = inc_dir / "defs_11.vh"
        defs_vh.write_text("`define CONST_OFFSET 10\n", encoding="utf-8")

        top_sv = self.workdir / "top_11.sv"
        top_sv.write_text("""
`include "defs_11.vh"
module tb_11;
    initial begin
        int val = `BASE_VAL + `CONST_OFFSET;
        $display("TEST_11_F_SUCCESS: %0d", val);
    end
endmodule
""", encoding="utf-8")

        filelist_f = self.workdir / "run_11.f"
        filelist_f.write_text(f"""
# Line comment with hash
// Line comment with double slash
/* Multi-line
   block comment */
+incdir+{inc_dir}
+define+BASE_VAL=42
{top_sv}
""", encoding="utf-8")

        out_cpp = self.workdir / "out_11_f.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            "-f", str(filelist_f),
            "-o", str(out_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp -f failed:\n{res.stderr}")
        self.assertTrue(out_cpp.exists(), "Output C++ file not created")
        stdout, stderr, code = self.compile_and_run(out_cpp, "run_11_f")
        self.assertEqual(code, 0, f"Execution failed:\n{stderr}")
        self.assertIn("TEST_11_F_SUCCESS: 52", stdout)

        # 2. Test -F <filelist.f> resolving relative paths relative to filelist location
        sub_dir = self.workdir / "nested_sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        sub_sv = sub_dir / "sub_top.sv"
        sub_sv.write_text("""
module tb_sub;
    initial begin
        int sum = 0;
        for (int i = 1; i <= 5; i++) begin
            sum += i;
        end
        $display("TEST_11_BIG_F_SUCCESS: sum=%0d", sum);
    end
endmodule
""", encoding="utf-8")

        filelist_big_f = sub_dir / "files.f"
        filelist_big_f.write_text("sub_top.sv\n", encoding="utf-8")
        out_sub_cpp = self.workdir / "out_11_big_f.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            "-F", str(filelist_big_f),
            "-o", str(out_sub_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(self.workdir))
        self.assertEqual(res.returncode, 0, f"sv2cpp -F failed:\n{res.stderr}")
        self.assertTrue(out_sub_cpp.exists())
        stdout, stderr, code = self.compile_and_run(out_sub_cpp, "run_11_big_f")
        self.assertEqual(code, 0)
        self.assertIn("TEST_11_BIG_F_SUCCESS: sum=15", stdout)

        # 3. Test multiple source files (package + top)
        pkg_sv = self.workdir / "pkg_11.sv"
        pkg_sv.write_text("""
package test_pkg_11;
    typedef struct {
        int id;
        int status;
    } packet_t;

    function automatic int calc_score(int base, int mult);
        return base * mult;
    endfunction
endpackage
""", encoding="utf-8")

        top_multi_sv = self.workdir / "top_multi_11.sv"
        top_multi_sv.write_text("""
import test_pkg_11::*;

module tb_multi_src;
    initial begin
        packet_t pkt;
        pkt.id = 101;
        pkt.status = calc_score(7, 6);
        $display("TEST_11_MULTI_SUCCESS: id=%0d status=%0d", pkt.id, pkt.status);
    end
endmodule
""", encoding="utf-8")

        out_multi_cpp = self.workdir / "out_11_multi.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(pkg_sv), str(top_multi_sv),
            "-o", str(out_multi_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp multi-source failed:\n{res.stderr}")
        stdout, stderr, code = self.compile_and_run(out_multi_cpp, "run_11_multi")
        self.assertEqual(code, 0)
        self.assertIn("TEST_11_MULTI_SUCCESS: id=101 status=42", stdout)

        # 4. Test stdout preview when -o is omitted
        top_preview_sv = self.workdir / "top_preview_11.sv"
        top_preview_sv.write_text("""
module tb_preview;
    initial begin
        $display("TEST_11_PREVIEW_SUCCESS");
    end
endmodule
""", encoding="utf-8")
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(top_preview_sv)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp stdout preview failed:\n{res.stderr}")
        self.assertIn("Generated Bare-metal C++ Code Preview", res.stdout)
        self.assertIn("int main(int argc, char **argv)", res.stdout)
        self.assertIn("TEST_11_PREVIEW_SUCCESS", res.stdout)

    def test_12_process_selection(self):
        """Test target process selection by line number, @line, named block label, and invalid spec."""
        sv_file = self.workdir / "multi_target.sv"
        sv_content = """module tb_multi_proc;
    int counter = 0;

    // Process 0: default first process
    initial begin
        counter = 100;
        $display("ACTIVE_PROC: FIRST_DEFAULT (counter=%0d)", counter);
    end

    // Process 1: named block process
    initial begin : worker_proc
        counter = 200;
        $display("ACTIVE_PROC: WORKER_LABEL (counter=%0d)", counter);
    end

    // Process 2: line-targeted process
    initial begin
        counter = 300;
        $display("ACTIVE_PROC: THIRD_PROCESS (counter=%0d)", counter);
    end
endmodule
"""
        sv_file.write_text(sv_content, encoding="utf-8")

        lines = sv_content.splitlines()
        third_line = None
        for idx, line in enumerate(lines, 1):
            if "counter = 300" in line:
                third_line = idx - 1
                break
        self.assertIsNotNone(third_line)

        # 1. Plain line number
        out_line_cpp = self.workdir / "out_12_line.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(sv_file),
            "--process", str(third_line),
            "-o", str(out_line_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp by line failed:\n{res.stderr}")
        stdout, stderr, code = self.compile_and_run(out_line_cpp, "run_12_line")
        self.assertEqual(code, 0)
        self.assertIn("ACTIVE_PROC: THIRD_PROCESS (counter=300)", stdout)

        # 2. Explicit @line
        out_at_cpp = self.workdir / "out_12_at.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(sv_file),
            "--process", f"@{third_line}",
            "-o", str(out_at_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp by @line failed:\n{res.stderr}")
        stdout, stderr, code = self.compile_and_run(out_at_cpp, "run_12_at")
        self.assertEqual(code, 0)
        self.assertIn("ACTIVE_PROC: THIRD_PROCESS (counter=300)", stdout)

        # 3. Named block label
        out_lbl_cpp = self.workdir / "out_12_label.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(sv_file),
            "--process", "worker_proc",
            "-o", str(out_lbl_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp by label failed:\n{res.stderr}")
        stdout, stderr, code = self.compile_and_run(out_lbl_cpp, "run_12_label")
        self.assertEqual(code, 0)
        self.assertIn("ACTIVE_PROC: WORKER_LABEL (counter=200)", stdout)

        # 4. Invalid target process reports error
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(sv_file),
            "--process", "nonexistent_proc_xyz",
            "-o", str(self.workdir / "dummy.cpp")
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Process matching 'nonexistent_proc_xyz' not found", res.stdout + res.stderr)

    def test_13_embedding_modes_and_thresholds(self):
        """Test asset embedding modes (Mode A: C++ array, Mode B: .incbin, Mode C: fixed address, Auto partitioning)."""
        # Mode A: C++ static const uint8_t byte array
        file_a = self.workdir / "data_a.bin"
        file_a.write_bytes(bytes([0x10, 0x20, 0x30, 0x40]))
        sv_a = """
module tb_embed_a;
    initial begin
        int fd = $fopen("data_a.bin", "r");
        if (fd != 0) begin
            $fclose(fd);
            $display("MODE_A_SUCCESS");
        end
    end
endmodule
"""
        analyzer, out_a_cpp, cpp_code_a = self.transpile_sv(sv_a, "test_embed_a", embed_mode="a")
        self.assertIn("s_asset_data_a_bin", cpp_code_a)
        self.assertIn("0x10, 0x20, 0x30, 0x40", cpp_code_a)
        self.assertIn("g_auto_assets", cpp_code_a)
        stdout, stderr, code = self.compile_and_run(out_a_cpp, "run_embed_a")
        self.assertEqual(code, 0)
        self.assertIn("MODE_A_SUCCESS", stdout)

        # Mode B: GCC/Clang .incbin assembly directive
        file_b = self.workdir / "data_b.bin"
        file_b.write_bytes(bytes([0xAA, 0xBB, 0xCC, 0xDD]))
        sv_b = """
module tb_embed_b;
    initial begin
        int fd = $fopen("data_b.bin", "r");
        if (fd != 0) begin
            $fclose(fd);
            $display("MODE_B_SUCCESS");
        end
    end
endmodule
"""
        analyzer, out_b_cpp, cpp_code_b = self.transpile_sv(sv_b, "test_embed_b", embed_mode="b")
        self.assertIn(".incbin", cpp_code_b)
        self.assertIn("INCBIN_ASSET", cpp_code_b)
        self.assertIn("data_b.bin", cpp_code_b)
        stdout, stderr, code = self.compile_and_run(out_b_cpp, "run_embed_b")
        self.assertEqual(code, 0)
        self.assertIn("MODE_B_SUCCESS", stdout)

        # Mode C: Fixed DRAM address pointers
        file_c = self.workdir / "data_c.bin"
        file_c.write_bytes(bytes([1, 2, 3, 4]))
        sv_c = """
module tb_embed_c;
    initial begin
        int fd = $fopen("data_c.bin", "r");
        if (fd != 0) begin
            $fclose(fd);
            $display("MODE_C_SUCCESS");
        end
    end
endmodule
"""
        analyzer, out_c_cpp, cpp_code_c = self.transpile_sv(sv_c, "test_embed_c", embed_mode="c")
        self.assertIn("0x80000000", cpp_code_c)
        self.assertIn("g_auto_assets", cpp_code_c)
        stdout, stderr, code = self.compile_and_run(out_c_cpp, "run_embed_c")
        self.assertEqual(code, 0)
        self.assertIn("MODE_C_SUCCESS", stdout)

        # Auto Mode: Size threshold partitioning (<=256 -> A, 257..1024 -> B, >1024 -> C)
        part_a = self.workdir / "part_a.bin"
        part_a.write_bytes(b"A" * 100)
        part_b = self.workdir / "part_b.bin"
        part_b.write_bytes(b"B" * 500)
        part_c = self.workdir / "part_c.bin"
        part_c.write_bytes(b"C" * 2000)

        sv_part = """
module tb_embed_partition;
    initial begin
        int fd_a = $fopen("part_a.bin", "r");
        int fd_b = $fopen("part_b.bin", "r");
        int fd_c = $fopen("part_c.bin", "r");
        if (fd_a != 0) $fclose(fd_a);
        if (fd_b != 0) $fclose(fd_b);
        if (fd_c != 0) $fclose(fd_c);
        $display("PARTITION_SUCCESS");
    end
endmodule
"""
        analyzer, out_part_cpp, cpp_code_part = self.transpile_sv(
            sv_part, "test_embed_partition",
            embed_mode="auto",
            embed_threshold_a=256,
            embed_threshold_b=1024
        )
        self.assertIn("static const uint8_t s_asset_part_a_bin[100] = {", cpp_code_part)
        self.assertIn("INCBIN_ASSET(s_asset_part_b_bin,", cpp_code_part)
        self.assertIn("ASSET_ADDR_s_asset_part_c_bin", cpp_code_part)
        self.assertIn("ASSET_PTR_s_asset_part_c_bin", cpp_code_part)
        stdout, stderr, code = self.compile_and_run(out_part_cpp, "run_embed_partition")
        self.assertEqual(code, 0)
        self.assertIn("PARTITION_SUCCESS", stdout)

    def test_14_yaml_configuration(self):
        """Test YAML configuration file options (include_dirs, defines, variable_mappings, reachability pruning)."""
        inc_dir = self.workdir / "yaml_inc"
        inc_dir.mkdir(parents=True, exist_ok=True)
        hdr = inc_dir / "yaml_header.vh"
        hdr.write_text("`define YAML_MAGIC 777\n", encoding="utf-8")

        top_sv = self.workdir / "top_yaml.sv"
        top_sv.write_text("""
`include "yaml_header.vh"
module tb_yaml;
    int unmapped_var = 10;
    int hw_status_reg;

    task step_work();
        unmapped_var += 5;
    endtask

    // Uncalled simulation harness tasks should be pruned by reachability analysis
    task unused_clk_gen();
        #10;
    endtask

    initial begin
`ifdef YAML_FEATURE_ACTIVE
        int val = `YAML_MAGIC;
`else
        int val = 0;
`endif
        step_work();
        hw_status_reg = 32'h55aa_1122;
        $display("YAML_SUCCESS: val=%0d unmapped=%0d status=0x%08x", val, unmapped_var, hw_status_reg);
    end
endmodule
""", encoding="utf-8")

        cfg_file = self.workdir / "cfg.yaml"
        cfg_file.write_text(f"""
include_dirs:
  - "{inc_dir}"
defines:
  - "YAML_FEATURE_ACTIVE"
variable_mappings:
  hw_status_reg: "g_mapped_status"
extra_declarations:
  - "static uint32_t g_mapped_status = 0;"
embed_mode: "auto"
""", encoding="utf-8")

        out_cpp = self.workdir / "out_yaml.cpp"
        cmd = [
            sys.executable, str(ROOT_DIR / "sv2cpp.py"),
            str(top_sv),
            "-c", str(cfg_file),
            "-o", str(out_cpp)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sv2cpp with YAML config failed:\n{res.stderr}")
        cpp_code = out_cpp.read_text(encoding="utf-8")

        # Reachability checks: step_work is included, unused_clk_gen is pruned
        self.assertIn("step_work", cpp_code)
        self.assertNotIn("unused_clk_gen", cpp_code)
        # Variable mapping check: hw_status_reg is mapped to g_mapped_status
        self.assertIn("g_mapped_status = static_cast<int32_t>(0x55aa1122);", cpp_code)
        self.assertIn("static uint32_t g_mapped_status = 0;", cpp_code)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_yaml")
        self.assertEqual(code, 0, f"Execution failed:\n{stderr}")
        self.assertIn("YAML_SUCCESS: val=777 unmapped=15 status=0x55aa1122", stdout)

    def test_15_readmemb_and_advanced_io(self):
        """Test $readmemh with comments and @address jumps, and $readmemb binary format loading."""
        hex_content = """// Comment header line
10 2a 3f 4e
/* Block comment */
@04 55 66 77 88
@0a ff
"""
        hex_file = self.workdir / "data_15.hex"
        hex_file.write_text(hex_content, encoding="utf-8")

        bin_content = """// Binary test file
00000001
00000010
00000100
00001000
@04 11110000
11111111
"""
        bin_file = self.workdir / "data_15.bin"
        bin_file.write_text(bin_content, encoding="utf-8")

        sv = """
module tb_readmem;
    logic [7:0] hex_mem [0:15];
    logic [7:0] bin_mem [0:7];

    initial begin
        for (int i = 0; i < 16; i++) begin
            hex_mem[i] = 8'h00;
        end
        for (int i = 0; i < 8; i++) begin
            bin_mem[i] = 8'h00;
        end

        $readmemh("data_15.hex", hex_mem);
        $readmemb("data_15.bin", bin_mem);

        $display("HEX_MEM: [0]=0x%02x [1]=0x%02x [2]=0x%02x [3]=0x%02x [4]=0x%02x [5]=0x%02x [6]=0x%02x [7]=0x%02x [10]=0x%02x",
                 hex_mem[0], hex_mem[1], hex_mem[2], hex_mem[3], hex_mem[4], hex_mem[5], hex_mem[6], hex_mem[7], hex_mem[10]);
        $display("BIN_MEM: [0]=%0d [1]=%0d [2]=%0d [3]=%0d [4]=0x%02x [5]=0x%02x",
                 bin_mem[0], bin_mem[1], bin_mem[2], bin_mem[3], bin_mem[4], bin_mem[5]);
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_readmem")
        self.assertIn("mem_readmemh(\"data_15.hex\",", cpp_code)
        self.assertIn("mem_readmemb(\"data_15.bin\",", cpp_code)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_readmem")
        self.assertEqual(code, 0, f"Execution failed:\n{stderr}")
        self.assertIn("HEX_MEM: [0]=0x10 [1]=0x2a [2]=0x3f [3]=0x4e [4]=0x55 [5]=0x66 [6]=0x77 [7]=0x88 [10]=0xff", stdout)
        self.assertIn("BIN_MEM: [0]=1 [1]=2 [2]=4 [3]=8 [4]=0xf0 [5]=0xff", stdout)

    def test_16_comments_and_delay_salvaging(self):
        """Test comment preservation, simulation timing construct salvaging, and multi-value case statements."""
        sv = """
module tb_comments_and_timing;
    logic clk;

    function automatic int classify(int val);
        int res;
        case (val)
            1: res = 10;
            2, 3: begin
                res = 25;
            end
            4: begin
                res = 40;
                return res;
            end
            default: res = -1;
        endcase
        return res;
    endfunction

    initial begin
        // Single-line note: setting initial parameters
        int a = 0;
        int b = 0;
        int c = 0;

        /* Multi-line block note:
           Testing delay salvaging */
        #10 a = 123;

        @(posedge clk) begin
            b = 456;
            c = 789;
        end

        $display("SALVAGE: a=%0d b=%0d c=%0d", a, b, c);
        $display("CLASSIFY: r1=%0d r2=%0d r3=%0d r4=%0d r5=%0d",
                 classify(1), classify(2), classify(3), classify(4), classify(99));
    end
endmodule
"""
        analyzer, out_cpp, cpp_code = self.transpile_sv(sv, "test_comments_timing")
        # Check comments preserved
        self.assertIn("Single-line note: setting initial parameters", cpp_code)
        self.assertIn("Multi-line block note:", cpp_code)
        self.assertIn("Testing delay salvaging", cpp_code)

        # Check timing construct warnings
        self.assertIn("/* [WARNING: Simulation timing construct ignored]", cpp_code)

        # Check multi-value case statement transpilation
        self.assertIn("switch (val)", cpp_code)
        self.assertIn("case 1:", cpp_code)
        self.assertIn("case 2:", cpp_code)
        self.assertIn("case 3:", cpp_code)
        self.assertIn("default:", cpp_code)

        stdout, stderr, code = self.compile_and_run(out_cpp, "run_comments_timing")
        self.assertEqual(code, 0, f"Execution failed:\n{stderr}")
        self.assertIn("SALVAGE: a=123 b=456 c=789", stdout)
        self.assertIn("CLASSIFY: r1=10 r2=25 r3=25 r4=40 r5=-1", stdout)


if __name__ == "__main__":
    unittest.main()
