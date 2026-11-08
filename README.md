# sv2cpp - SystemVerilog to Bare-metal / Embedded C++ Transpiler

SystemVerilog のテストベンチやプロシージャルコード（`initial begin...end`、タスク/関数群）を解析し、ベアメタル環境・組込み・エミュレーション・高速シミュレーション向けにモダン C++（デフォルト C++17）へトランスパイルするツールスイートです。

---

## ディレクトリ構成

```text
sv2cpp/
├── sv2cpp.py                 # トランスパイラ本体スクリプト
├── runtime/                  # C++ ランタイムライブラリ
│   ├── sv_runtime.hpp        # ランタイムヘッダ（plusargs, mem_file, 演算子ヘルパー）
│   └── sv_runtime.cpp        # ランタイム実装
├── configs/                  # 回路別マッピング設定ファイル（YAML）
│   └── jpeg_tool.yaml        # JPEG 用タスク・変数マッピング設定例
├── examples/                 # サンプル・実証プロジェクト
│   ├── jpeg/                 # JPEG デコーダ実証用 RTL & テストベンチ
│   ├── dummy_hal.c           # サンプル用ダミー HAL（WriteReg / ReadReg）
│   └── sample_user_assets.c  # 動的アセット手動登録例
├── tests/                    # ユニットテスト & 統合テスト
│   └── test_cpp_transpiler.py
├── transpile_jpeg.sh         # JPEG サンプルのワンコマンド実行スクリプト
├── Makefile                  # テスト実行・管理用
└── requirements.txt          # Python 依存パッケージ (pyslang, pyyaml)
```

---

## 主な特徴・機能

1. **Slang AST 解析 & EDA 互換フロントエンド**:
   - `pyslang` を採用し、SystemVerilog の構文・AST・マクロ・型を正確に解析。
   - `-f` / `-F` ファイルリスト、`+incdir+` / `-I`、`+define+` / `-D` などの EDA 標準オプションにネイティブ対応。
2. **モダン C++17 コード生成**:
   - `output` / `inout` / `ref` 引数を自然な C++ 参照渡し（`Type&`）へ変換。
   - SystemVerilog キュー（`[$]`）を `std::vector`、文字列を `std::string` へマッピング。
   - 自然な演算子順位によるクリーンな式出力（不要な多重括弧を抑制しつつ、結合性や防御が必要な箇所のみ括弧を付与）。
   - Verilog 繰り返し演算子 `{ N{ val } }`（定数畳み込み、1-bit broadcast、マルチビット連結）に対応。
3. **インクルードファイル (`.vh`) のインプレース C++ ヘッダ復元 (`.h`)**:
   - `include` されたヘッダ内のマクロ（`define`）、定数（`parameter` / `localparam`）、構造体（`typedef struct`）、タスク/関数を C++ 形式へ変換。
   - GCC/Clang 準拠の 3 条件インクルードガード検出により、ネストした `` `ifdef `` や末尾のディレクティブを壊さずそのまま復元。
4. **到達可能性解析（Call Graph）による枝刈り**:
   - 指定した `initial begin...end` プロセスを起点に、呼び出されるタスク/関数のみを依存解析して出力。
   - HAL API（レジスタアクセス関数等）境界で枝刈り（Pruning）し、クロック待ちやピン駆動ハーネスなどのシミュレーション専用コードを安全に自動除外。
5. **ROMFS アセット自動埋め込み (`--embed-mode`)**:
   - `$fopen` / `$fread` / `$readmemh` 等で参照されるファイルを静的追跡し、サイズに応じて C 配列 (`auto` / `a`)、アセンブリ `.incbin` (`b`)、固定アドレスポインタ (`c`) へ自動配置。
6. **ベアメタル / Plusargs ランタイム (`runtime/`)**:
   - `$test$plusargs` / `$value$plusargs` のコマンドライン引数処理。
   - ファイルシステムの存在しない組込み環境でのメモリ上ストリーム I/O（ROMFS）。
   - `$display`, `$sformatf`, `$finish`, `$time` などのシステムタスクをサポート。

---

## セットアップ

### 動作要件
- Python 3.8 以上
- C++17 対応コンパイラ（`g++` または `clang++`）

### インストール方法

```bash
# 仮想環境を使用する場合
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# または、ローカルディレクトリ（pyslang_env/）へ直接インストールする場合
pip install -r requirements.txt --target=pyslang_env
```
※ `sv2cpp.py` は同一ディレクトリの `pyslang_env/` を自動認識するため、仮想環境なしでも実行可能です。

---

## 基本的な使い方

### 1. トランスパイル

```bash
# EDA ファイルリスト (-f) を使用する場合
python3 sv2cpp.py -f examples/jpeg/sim/tool_sim.f \
    -c configs/jpeg_tool.yaml \
    --process 0 \
    -o build/test.cpp

# 直接引数 (+incdir+, +define+) を指定する場合
python3 sv2cpp.py examples/jpeg/sim/jpeg_tool_tb.sv \
    +incdir+examples/jpeg/rtl \
    +define+TEST_MODE=1 \
    -c configs/jpeg_tool.yaml \
    --process 0 \
    -o build/test.cpp
```

### 2. コンパイル & 実行

```bash
# C++17 でビルド（ランタイムおよび HAL 実装とリンク）
g++ -O2 -std=c++17 \
    -Iruntime \
    -Ibuild \
    build/test.cpp \
    runtime/sv_runtime.cpp \
    examples/dummy_hal.c \
    -o build/test_sim

# 実行（+plusarg 引数を指定可能）
./build/test_sim +JOB_DIR=job
```

### 3. 実証サンプルのワンコマンド実行

JPEG デコーダ検証環境をトランスパイル・ビルド・実行までワンステップで行うスクリプトが用意されています。

```bash
./transpile_jpeg.sh --run
```

---

## コマンドライン引数リファレンス

### 構文
```bash
python3 sv2cpp.py [sv2cpp オプション] [EDA/slang オプション: -f, +incdir+, +define+, ソース...]
```

### sv2cpp 固有オプション

| オプション | 引数 | 説明 | デフォルト |
|---|---|---|---|
| `-h`, `--help` | - | ヘルプメッセージを表示して終了します。 | - |
| `-c`, `--config` | `FILE` | HAL API マッピングや変数置換を定義した YAML 設定ファイル。 | なし |
| `--process` | `PROC` | 変換対象の `initial begin...end` プロセス（インデックス `0`、行番号、またはブロック名）。 | 最初のプロセス (`0`) |
| `--cpp-std` | `c++11`, `c++14`, `c++17`, `c++20` | 出力する C++ 言語規格。 | `c++17` |
| `--embed-mode` | `auto`, `a`, `b`, `c` | アセットデータの埋め込み方式（詳細は下記参照）。 | `auto` |
| `--embed-threshold-a` | `BYTES` | `auto` モード時にモード A（C 配列）として埋め込む最大ファイルサイズ。 | `256` |
| `--embed-threshold-b` | `BYTES` | `auto` モード時にモード B（`.incbin`）として埋め込む最大ファイルサイズ。 | `1024` |
| `--workdir`, `--cwd` | `DIR` | ファイルパス解決の基準ワーキングディレクトリ。 | `.` (カレントディレクトリ) |
| `-o`, `--output` | `FILE` | 出力先 C++ ソースファイルのパス（省略時は標準出力へプレビュー表示）。 | なし (stdout) |

#### データ埋め込みモード（`--embed-mode`）
- `auto`: ファイルサイズに応じて方式を自動選択（$\le$ 256B: モード A、$\le$ 1024B: モード B、$>$ 1024B: モード C）。
- `a` (C Array): すべてのアセットを `static const uint8_t` 配列としてソース内に埋め込みます。
- `b` (Assembly Incbin): GCC / Clang の `.incbin` ディレクティブを用い、バイナリを直接 `.rodata` に配置します。
- `c` (Fixed Address): フラッシュ/ROM/DRAM 上の固定メモリアドレス（ポインタ）参照として出力します。

---

### EDA / Slang 標準オプション

| オプション | 説明 |
|---|---|
| `-f <FILE>` | ファイルリスト（ソースファイル一覧、インクルードパス、マクロ定義）を読み込みます。 |
| `-F <FILE>` | `-f` と同様ですが、相対パスがファイルリスト自身のディレクトリ基準となります。 |
| `+incdir+<DIR>`, `-I<DIR>` | `` `include `` の探索ディレクトリを追加します。 |
| `+define+<M>[=<V>]`, `-D<M>[=<V>]` | プリプロセッサマクロを定義します。 |
| `<sources.sv>` | 解析対象の SystemVerilog / Verilog ソースファイル。 |

---

## 設定ファイル（YAML）リファレンス

`-c` / `--config` で指定する YAML 設定ファイルにより、ターゲット C++ 環境に応じたハードウェア API 置換や不要コードの枝刈りを制御します。

### 設定項目一覧

| キー | 型 | 説明 |
|---|---|---|
| `task_mappings` | `dict` | SV タスク/関数を C/C++ の HAL API（レジスタアクセス関数等）に置き換えるマッピング定義。ここで指定されたタスクは末端（Leaf）として扱われ、内部実装の探索（枝刈り）を停止します。 |
| `variable_mappings` | `dict` | テストベンチ内のモジュール変数参照を、C++ 側の式・関数呼び出し（例: `get_cycle_count()`）に置き換えるマッピング定義。 |
| `extra_declarations` | `list` | 生成される C++ ソースの先頭（プロトタイプ宣言部）に挿入される追加コード（`extern` 宣言、インライン関数スタブ等）。 |
| `embed_mode` | `string` | アセットデータの埋め込み方式（`"auto"`, `"a"`, `"b"`, `"c"`）。 |
| `include_dirs` | `list` | `` `include `` 探索ディレクトリリスト（EDA の `+incdir+` 相当）。 |
| `defines` | `list` | プリプロセッサマクロ定義リスト（EDA の `+define+` 相当）。 |

### YAML 設定例

```yaml
# configs/jpeg_tool.yaml
task_mappings:
  apb_wr: "WriteReg"              # apb_wr(addr, val, err) -> WriteReg(addr, val, err)
  apb_rd: "ReadReg"               # apb_rd(addr, val, err) -> ReadReg(addr, val, err)
  bus_reset: "ResetPeripheral"    # bus_reset() -> ResetPeripheral()
  bus_tick: "Stub_BusTick"        # bus_tick() -> Stub_BusTick()

variable_mappings:
  cycle_cnt: "get_cycle_count()"  # cycle_cnt の参照を取得関数に置換

extra_declarations:
  - "extern uint64_t get_cycle_count(void);"
  - "static inline void Stub_BusTick(void) {}"

embed_mode: "auto"

include_dirs:
  - rtl

defines:
  - TEST_MODE=1
```

---

## テストの実行

リポジトリ内の全ユニットテストおよび統合テストは `Makefile` 経由で実行できます。

```bash
# 全ユニットテスト & 統合テストの実行（16テストスイート）
make test
```

### 主なテスト検証項目 (`tests/test_cpp_transpiler.py`)
- リテラル、演算子順位、繰り返し演算子 `{ N{ val } }`
- `std::string`, `$sformat`, `$sformatf`
- キュー（`[$]`）と `std::vector` メソッド
- `output` / `inout` / `ref` の C++ 参照渡し（`Type&`）
- `typedef struct` およびフィールドアクセス
- 制御構文（`if`, `case`, `while`, `for`, `repeat`, `do-while`）
- HAL API マッピングと `+plusargs`
- `$fopen`, `$fscanf`, `$readmemh`, ROMFS アセット埋め込み
- インクルードファイル復元（`.vh` $\to$ `.h`、多重・末尾 `ifdef` / インクルードガード維持）
