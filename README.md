# sv2c - SystemVerilog to Bare-metal C Transpiler

SystemVerilog のテストベンチやプロシージャルコード（`initial begin...end`、タスク/関数群）を解析し、ベアメタル環境（C言語）向けにトランスパイルする汎用ツールスイートです。

## ディレクトリ構成

```text
sv2c/
├── sv2c.py                 # トランスパイラ本体スクリプト
├── pyslang_env/            # 独立Python実行環境（pyslang, pyyaml）
├── runtime/                # ベアメタル用I/Oランタイム
│   ├── mem_file.h          # メモリファイル/ROMFS I/Oヘッダ
│   ├── mem_file.c          # メモリファイル/ROMFS 実装
│   ├── plusargs.h          # SystemVerilog plusargs ランタイムヘッダ
│   ├── plusargs.c          # SystemVerilog plusargs 実装
│   ├── test_mem_file.c     # mem_file 単体テスト
│   └── test_plusargs.c     # plusargs 単体テスト
├── configs/                # 回路別マッピング設定ファイル（YAML）
│   └── jpeg_tool.yaml      # JPEG用タスク・変数マッピング設定例
├── examples/               # 動作確認・サンプル
│   ├── dummy_hal.c         # サンプル用ダミーHAL（WriteReg / ReadReg）
│   └── test_job/           # テスト用ジョブデータ（config.txt, q0.bin 等）
├── build/                  # sv2c 用ビルド・出力作業ディレクトリ
└── README.md               # 本ドキュメント
```

## 主な機能

1. **抽象構文木（AST）解析 & プリプロセッサマクロ対応**:
   - `pyslang` を用いて SystemVerilog ソースコードを正確に解析。
   - `+define+NAME=VAL`, `-D NAME=VAL` による条件コンパイル（`` `ifdef ``）の正確な解決。
   - EDA標準の引数ファイル（`-f <filelist.f>`）を直接受け付け、ソースファイル・インクルードパス・マクロ定義を一括展開。
2. **到達可能性解析（Call Graph）による枝刈り**:
   - 指定した `initial begin...end` プロセスを起点に呼び出されるタスク/関数のみを走査。
   - レジスタアクセス等のハードウェア境界タスク（`WriteReg` 等）で枝刈り（Pruning）し、シミュレーション用ハーネス（クロックトグルやバス模擬回路）を安全に完全除外。
3. **AST動的ファイル名の静的追跡（ROMFS自動生成）**:
   - `$sformat` や文字列結合、ループ変数、三項演算子のデータフローを追跡し、アクセスされるファイルパス群を自動列挙。
   - `MemAssetEntry g_auto_assets[]` テーブルおよび実データ配列を C ソース内に全自動展開。
4. **ベアメタル I/O & Plusargs ランタイム**:
   - `mem_file`: ファイルシステムの存在しないベアメタル環境で、`$fopen`, `$fread`, `$fscanf` 等の呼び出しをメモリ上ストリームへ安全にリダイレクト。
   - `plusargs`: SystemVerilog の `$test$plusargs` / `$value$plusargs` を `sv_test_plusargs()` / `sv_value_plusargs()` にマッピング。Cの `argc/argv`、UART/プロンプト入力文字列、デフォルト値から引数（`+JOB_DIR=...` 等）を直接受け取り可能。

## セットアップ（環境構築）

`sv2c` は SystemVerilog AST パーサーの `pyslang` と設定ファイル読み込みの `pyyaml` を使用します。
本ツールは外部パッケージをローカルディレクトリ（`sv2c/pyslang_env`）に自己内包する方式、または通常の Python 仮想環境（`.venv`）のいずれでも動作します（`pyslang_env` は Git 管理対象外です）。

### 方法 A: ローカル完結型セットアップ（推奨）

仮想環境のアクティベート（`source ...`）不要で手軽に実行したい場合、`--target` オプションで `sv2c/pyslang_env` にパッケージを直接インストールします（Makefile からワンコマンドで実行可能です）。

```bash
# Makefile を使用する場合
make sv2c-setup

# または pip コマンドを直接実行する場合
pip install -r sv2c/requirements.txt --target=sv2c/pyslang_env
```
※ `sv2c.py` は自動的に `sv2c/pyslang_env` を検知して読み込むため、これだけで `python3 sv2c/sv2c.py` が実行可能になります。

---

### 方法 B: 標準の Python 仮想環境（venv）セットアップ

標準的な Python ワークフローを使用したい場合は、通常の仮想環境を作成してインストールします。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r sv2c/requirements.txt
```

---

## 基本的な使い方

```bash
# 1. トランスパイル実行（-f filelist.f を指定する場合）
python3 sv2c/sv2c.py -f sv2c/examples/tool_sim.f \
    -c sv2c/configs/jpeg_tool.yaml \
    --process 0 \
    -o sv2c/build/sv2c_test_gen.c

# または CLI で直接 EDA 引数を指定する場合
python3 sv2c/sv2c.py sim/jpeg_tool_tb.sv \
    +incdir+rtl \
    +define+TEST_MODE=1 \
    -c sv2c/configs/jpeg_tool.yaml \
    --process 0 \
    -o sv2c/build/sv2c_test_gen.c

# 2. コンパイル（Linux / ホストでの動作検証例）
gcc -Wall -Wextra -Wno-unused-variable -Wno-unused-parameter \
    -I sv2c/runtime \
    sv2c/build/sv2c_test_gen.c \
    sv2c/runtime/mem_file.c \
    sv2c/runtime/plusargs.c \
    sv2c/examples/dummy_hal.c \
    -o sv2c/build/run_test_gen

# 3. 実行（シミュレータと同じ形式の +plusarg が使用可能）
./sv2c/build/run_test_gen +JOB_DIR=job
```

## コマンドライン引数リファレンス

### 構文
```bash
python3 sv2c/sv2c.py [sv2c オプション] [EDA/slang オプション: -f, +incdir+, +define+, ソース...]
```

### sv2c 固有オプション

| オプション | 引数 | 説明 | デフォルト |
|---|---|---|---|
| `-h`, `--help` | - | 引数のヘルプ・使用例を表示して終了します。 | - |
| `-c`, `--config` | `FILE` | ペリフェラルAPIマッピング、変数置換等を定義した YAML 設定ファイルのパス。 | なし |
| `--process` | `PROC` | 変換対象の `initial begin...end` プロセス。インデックス（例: `0`）、行番号（例: `576`）、またはブロック名/ラベルで指定します。 | 最初のプロセス (`0`) |
| `--embed-mode` | `auto`, `a`, `b`, `c` | ROMFS アセットデータの埋め込み方式を指定します（詳細は下記参照）。 | `auto` |
| `--embed-threshold-a` | `BYTES` | `auto` モード時にモード A（C 配列）として埋め込む最大ファイルサイズ。 | `256` |
| `--embed-threshold-b` | `BYTES` | `auto` モード時にモード B（`.incbin`）として埋め込む最大ファイルサイズ。これを超えるサイズはモード C（固定アドレス）となります。 | `1024` |
| `--workdir`, `--cwd` | `DIR` | シミュレーション実行時の基準ワーキングディレクトリ。静的確定パスの探索基準となります。 | `.` (カレントディレクトリ) |
| `-o`, `--output` | `FILE` | 出力先 C ソースファイルのパス。省略時は標準出力（stdout）にプレビューを表示します。 | なし (stdout) |

#### データ埋め込みモード（`--embed-mode`）の詳細:
- `auto`: ファイルサイズに基づき各アセットを最適方式へ自動振り分け：
  - **$\le$ 256 bytes** (`--embed-threshold-a`): **モード A (C Array)**
  - **257 $\sim$ 1024 bytes** (`--embed-threshold-b`): **モード B (Assembly .incbin)**
  - **$>$ 1024 bytes**: **モード C (Fixed Address)**
- `a` (C Array): すべてのアセットを `static const uint8_t s_asset_...[]` C 配列としてソース内に埋め込みます。
- `b` (Assembly Incbin): `.incbin` ディレクティブを用いたアセンブリ埋め込み形式を出力します。
- `c` (Fixed Address): フラッシュ/ROM/DRAM 上の固定メモリアドレス（ポインタ）参照として出力します。

#### 動的ファイルパス（`$value$plusargs` 依存）の扱いについて:
SystemVerilog 側で `$value$plusargs("JOB_DIR=%s", job_dir)` などの実行時引数を使ってファイルパスを動的生成している場合（例: `{job_dir, "/config.txt"}` や `$sformat(..., "%s/q%0d.bin", job_dir, ...)`）、トランスパイル時点ではフルパスを静的に確定できません。
- `sv2c` はこれを検知して **`[WARNING]`** を出力し、自動展開をスキップします。
- 出力される C ソース内に手動登録用のスケルトン（`g_user_assets[]`）が出力されますので、データを指定アドレスにロードした上でアセットテーブル（パスとメモリアドレス・サイズ）を定義してプログラムを実行してください。

#### シミュレーション専用構文（遅延・イベント・待機）の扱いについて:
Verilog の `#1;`（遅延）や `@(posedge clk);`（イベント待ち）、`wait (cond);`、`disable` などのシミュレーション専用構文は、ベアメタル C 言語のシーケンシャル実行モデルには直接の変換先が存在しません。
- `sv2c` はこれらを検知すると、解析レポート（セクション `[7]`）および C ソースのヘッダコメントに **`[WARNING]`** を出力します。
- C コード中には元の Verilog 構文をコメントとして残します（例: `/* [WARNING: Simulation timing construct ignored] #1; */`）。
- タイミング構文の内部に実実行文が含まれている場合（例: `#10 data = 1;`）、タイミング自体はコメント化され、実実行文（`data = 1;`）は保持されます。
- `repeat (N)` や `forever`、`do...while` などのループ構文は、C 言語の `for` / `while` ループへ自動変換されます。

---

### EDA / Slang 標準オプション（コンパイラ・フロントエンド）

slang のネイティブパーサーにより、一般的な EDA シミュレータ（VCS, Questa, Xcelium）と同一のオプションを直接指定できます。

| オプション | 説明 |
|---|---|
| `-f <FILE>` | ファイルリスト（ソースファイル一覧、インクルードパス、マクロ定義を記述したテキストファイル）を読み込みます。コメント（`#`, `//`, `/* */`）をサポートします。 |
| `-F <FILE>` | `-f` と同様ですが、ファイルリスト内の相対パスがそのファイルリスト自身のディレクトリ基準として解釈されます。 |
| `+incdir+<DIR>`, `-I<DIR>` | `` `include `` で探索するインクルードディレクトリを追加します。 |
| `+define+<MACRO>[=<VAL>]`, `-D<MACRO>[=<VAL>]` | プリプロセッサマクロを定義します（`` `ifdef `` 分岐の制御等）。 |
| `<sources.sv>` | 解析対象の SystemVerilog / Verilog ソースファイル。 |

---

### 設定ファイル（YAML）のリファレンス

`-c` / `--config` で指定する YAML 設定ファイルでは、SystemVerilog とターゲットベアメタル C 環境の差異を吸収するための各種マッピングやコンパイル条件を細かく制御できます。

#### サポートされている全設定項目一覧

| キー | 型 | デフォルト値 | 説明 |
|---|---|---|---|
| `task_mappings` | `dict` (マッピング) | `{"apb_wr": "WriteReg", ...}` | SystemVerilog タスク/関数をターゲット C の HAL API（レジスタアクセス関数等）に置き換えるマッピング定義。 |
| `variable_mappings` | `dict` (マッピング) | `{}` (空) | テストベンチ内のモジュール変数参照を、C側のアクセサ関数（マクロやタイマー関数）に置き換えるマッピング定義。 |
| `extra_declarations` | `list` (文字列配列) | `[]` (空) | 生成される C ソースの先頭（プロトタイプ宣言部）にそのまま挿入される追加コード（外部関数宣言 `extern`、インライン関数、マクロ等）。 |
| `embed_mode` | `string` | `"auto"` | 入力アセットデータ（`$fopen`, `$fread` 等で参照されるファイル）の C 埋め込み方式。`auto`, `a`, `b`, `c` から選択。 |
| `include_dirs` | `list` (文字列配列) | `[]` (空) | SystemVerilog 解析時に `` `include `` を探索するディレクトリリスト（EDA の `+incdir+` / `-I` に相当）。 |
| `defines` | `list` (文字列配列) | `[]` (空) | SystemVerilog プリプロセッサに渡すマクロ定義のリスト（EDA の `+define+` / `-D` に相当）。 |

---

#### 各設定項目の詳細

#### 1. `task_mappings`（タスク / 関数の HAL API マッピング）
- **役割**: ハードウェア境界タスク（APB/AXI バスアクセス、リセット発行等）をターゲットベアメタルの HAL（Hardware Abstraction Layer）関数に置き換えます。
- **動作**: マッピングされたタスクは、**到達可能性解析において「末端（Leaf）」として扱われ、その内部実装コードはトランスパイルされません**。これにより、クロック待機（`@(posedge clk)`）やピン駆動を行うシミュレーション専用ハーネスを安全に完全除外できます。呼び出し箇所の引数（値渡し・参照渡しポインタ）は自動的に適合されます。
- **記述例**:
  ```yaml
  task_mappings:
    apb_wr: "WriteReg"              # apb_wr(addr, val, err) -> WriteReg(addr, val, err)
    apb_rd: "ReadReg"               # apb_rd(addr, val, err) -> ReadReg(addr, &val, err)
    bus_reset: "ResetPeripheral"    # bus_reset() -> ResetPeripheral()
    bus_tick: "Stub_BusTick"        # bus_tick() -> Stub_BusTick()
  ```

#### 2. `variable_mappings`（モジュール変数の置換マッピング）
- **役割**: テストベンチモジュールの最上位で宣言されている変数（レジスタやカウンタ）を、ターゲット C 環境の式やマクロ、関数呼び出しに置き換えます。
- **動作**: ここに指定された変数は、C ソース内で参照されるたびに右辺の式（例: `get_cycle_count()`）へ置換されます。**ここに指定されていないモジュール変数は、Verilog のビット幅・符号・配列長に合わせて C の静的変数（`static uint32_t var_name;` 等）として自動宣言されます**。
- **記述例**:
  ```yaml
  variable_mappings:
    cycle_cnt: "get_cycle_count()"  # cycle_cnt の参照をタイマー取得関数に置換
    irq_status: "REG32(0x1000)"     # irq_status をメモリマップトレジスタ直接参照に置換
  ```

#### 3. `extra_declarations`（ヘッダ部への追加宣言・スタブ）
- **役割**: `task_mappings` や `variable_mappings` で指定した外部 C 関数・マクロのプロトタイプ宣言や、不要なタスクの空スタブ（インライン関数）を C ソースの先頭に直接出力します。
- **動作**: 指定した文字列が、生成される C ファイルのインクルード直後・関数プロトタイプ宣言部にそのまま出力されます。
- **記述例**:
  ```yaml
  extra_declarations:
    - "extern uint64_t get_cycle_count(void);"
    - "static inline void Stub_BusTick(void) {}"
    - "static inline void Stub_ApbAccess(uint16_t addr, bool write, uint32_t val, bool exp_err, uint32_t *rdata) { if (write) WriteReg(addr, val, exp_err); else ReadReg(addr, rdata, exp_err); }"
  ```

#### 4. `embed_mode`（アセットデータの埋め込みモード）
- **役割**: テストベンチ内の `$fopen` や `$fread`、`$readmemh` で読み込まれるテストデータや係数テーブル等のアセットを、C 言語/ベアメタル環境でどのように格納・参照するかを指定します。CLI オプション `--embed-mode` で上書き可能です。
- **選択肢**:
  - `auto`: （デフォルト）サイズに基づき最適方式を自動選択。
  - `a` (C Array): すべてのアセットを `static const uint8_t s_asset_...[]` C 配列としてソース内に完全展開します。任意の ANSI C99 コンパイラでコンパイル可能ですが、大容量データがある場合は C ソース行数が大きくなります。
  - `b` (Assembly Incbin): GCC / Clang のインラインアセンブリ `.incbin` ディレクティブを使用してバイナリを直接 `.rodata` セクションに配置します。C ソースサイズが約 70% 削減され、コンパイルが極めて高速になります。
  - `c` (Fixed Address): Flash / ROM / DRAM 上の固定メモリアドレス（デフォルト `ASSET_ROM_BASE + 0xXXXXXXXX`）へのポインタとして参照します。バイナリデータ実体を含まないため、実行ファイル自体のフットプリントが最小になります（コンパイル時マクロ `-DASSET_ROM_BASE=0xADDR` でベースアドレスを変更可能）。
- **記述例**:
  ```yaml
  embed_mode: "auto"  # "a", "b", "c", "auto"
  ```

#### 5. `include_dirs`（インクルード探索ディレクトリ）
- **役割**: SystemVerilog ソース内の `` `include "header.vh" `` を解決するための探索パスを指定します。
- **動作**: CLI 引数の `+incdir+<DIR>` または `-I<DIR>` と同じ順序で slang パーサーに渡されます。
- **記述例**:
  ```yaml
  include_dirs:
    - rtl
    - sim/include
  ```

#### 6. `defines`（プリプロセッサマクロ定義）
- **役割**: SystemVerilog コード内の `` `ifdef `` 分岐やパラメータを制御するためのマクロを定義します。
- **動作**: CLI 引数の `+define+<MACRO>[=<VAL>]` または `-D<MACRO>[=<VAL>]` と同じ形式で slang パーサーに渡されます。
- **記述例**:
  ```yaml
  defines:
    - TEST_MODE=1
    - SYNTHESIS
    - MAX_WIDTH=2048
  ```

---

### 完全な設定ファイル（YAML）の記述例

```yaml
# configs/top_tb.yaml
# SystemVerilog シミュレーションハーネスをベアメタル C に適合させる完全設定例

# 1. ハードウェア境界タスクのマッピング（シミュレーションハーネスの除外とHAL API呼び出しへの置換）
task_mappings:
  apb_wr: "WriteReg"
  apb_rd: "ReadReg"
  bus_reset: "ResetPeripheral"
  bus_tick: "Stub_BusTick"
  apb_access: "Stub_ApbAccess"

# 2. グローバルモジュール変数のマッピング（タイマー関数やペリフェラルレジスタへの置換）
variable_mappings:
  cycle_cnt: "get_cycle_count()"

# 3. 追加 C プロトタイプ宣言・インライン関数スタブ
extra_declarations:
  - "extern uint64_t get_cycle_count(void);"
  - "static inline void Stub_BusTick(void) {}"
  - "static inline void Stub_ApbAccess(uint16_t addr, bool write, uint32_t val, bool exp_err, uint32_t *rdata) { if (write) WriteReg(addr, val, exp_err); else ReadReg(addr, rdata, exp_err); }"

# 4. アセットデータ埋め込みモード ("auto", "a", "b", "c")
embed_mode: "auto"

# 5. インクルードディレクトリの指定
include_dirs:
  - rtl

# 6. プリプロセッサマクロ定義
defines:
  - TEST_MODE=1
```

---

## 7. テストスイートの実行方法 (`sv2c/Makefile`)

`sv2c` ディレクトリ内に自己完結したテスト実行用 Makefile を整備しています。外部テストフレームワーク（pytest 等）の追加インストールなしで、Python 標準の `unittest` および C コンパイラを用いて即座に回帰検証を実行できます。

### 実行コマンド

```bash
cd sv2c

# 1. ユニットテスト・統合テスト（インクルード復元・マクロ・型抽出・Cコンパイル実行）
make test

# 2. ベアメタルランタイムライブラリ（plusargs, mem_file）のテスト
make test-runtime

# 3. 全テスト（トランスパイラ＋ランタイム）の一括実行
make test-all
```

### 主なテスト検証項目 (`tests/test_include_restore.py`)
- **包含的インクルード復元**: マクロ（`define`）、定数（`parameter`/`localparam`）、構造体（`typedef struct`）、タスク（`task`）が単一の C ヘッダ（`.h`）へ分離出力され、メイン C ソースから呼び出されて正常動作することの検証。
- **手続きブロック内 include の警告検出**: `initial` や `task` の途中に `include された場合に、C99 規格に適合させるための自動警告（Warning）が発報され、C コンパイルが正常に通ることの検証。
- **実プロジェクト回帰テスト**: `sim/jpeg_tool_tb.sv` に対する sv2c 実行と生成コードの C 構文チェック（`gcc -fsyntax-only`）のゼロエラー検証。



