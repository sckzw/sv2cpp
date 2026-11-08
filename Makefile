# sv2c Makefile
# Standalone test runner and build management for SystemVerilog to Bare-metal C transpiler

PYTHON ?= python3
SV2C_DIR := $(shell pwd)
PYTHONPATH := $(SV2C_DIR):$(SV2C_DIR)/pyslang_env:$(PYTHONPATH)
export PYTHONPATH

.PHONY: all test test-cpp clean qemu-run qemu-clean help

all: test

help:
	@echo "sv2cpp Test Suite Makefile"
	@echo "=========================================================="
	@echo "  make test          Run all C++ transpiler (sv2cpp) unit tests"
	@echo "  make qemu-run SRC= Build and run arbitrary transpiled C++ file in QEMU"
	@echo "  make qemu-clean    Clean QEMU build artifacts"
	@echo "  make clean         Clean build artifacts and temporary files"
	@echo "=========================================================="

test: test-cpp

test-cpp:
	@echo "[sv2cpp] Running C++ Transpiler Unit & Integration Tests..."
	$(PYTHON) -m unittest tests/test_cpp_transpiler.py -v

qemu-run:
	$(MAKE) -C qemu run SRC=$(SRC)

qemu-clean:
	$(MAKE) -C qemu clean

clean: qemu-clean
	rm -rf build/unittest_* build/test_* build/run_* __pycache__ tests/__pycache__

