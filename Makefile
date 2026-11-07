# sv2c Makefile
# Standalone test runner and build management for SystemVerilog to Bare-metal C transpiler

PYTHON ?= python3
SV2C_DIR := $(shell pwd)
PYTHONPATH := $(SV2C_DIR):$(SV2C_DIR)/pyslang_env:$(PYTHONPATH)
export PYTHONPATH

.PHONY: all test test-unit test-cpp test-runtime test-all clean help

all: test

help:
	@echo "sv2c / sv2cpp Test Suite Makefile"
	@echo "=========================================================="
	@echo "  make test          Run all unit & integration tests"
	@echo "  make test-cpp      Run C++ transpiler (sv2cpp) unit tests"
	@echo "  make test-runtime  Run bare-metal C runtime library tests"
	@echo "  make test-all      Run full test suite (transpiler + runtime)"
	@echo "  make clean         Clean build artifacts and temporary files"
	@echo "=========================================================="

test: test-unit

test-cpp:
	@echo "[sv2cpp] Running C++ Transpiler Unit & Integration Tests..."
	$(PYTHON) -m unittest tests/test_cpp_transpiler.py -v

test-unit:
	@echo "[sv2c] Running Transpiler & Include Restoration Unit Tests..."
	$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

test-runtime:
	@echo "[sv2c] Building and Running Runtime Library Tests..."
	@mkdir -p build
	gcc -O2 -std=c99 -Iruntime runtime/plusargs.c runtime/test_plusargs.c -o build/test_plusargs
	./build/test_plusargs
	gcc -O2 -std=c99 -Iruntime runtime/mem_file.c runtime/test_mem_file.c -o build/test_mem_file
	./build/test_mem_file
	@echo "[sv2c] Runtime library tests passed successfully."

test-all: test-unit test-runtime
	@echo "\n[sv2c] All transpiler and runtime tests passed successfully!"

clean:
	rm -rf build/unittest_* build/test_* build/run_* __pycache__ tests/__pycache__

