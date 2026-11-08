# ====================================================================
# Bare-metal QEMU versatilepb Environment for sv2cpp Generated Tests
# Target CPU : ARM Cortex-R4 (runs on QEMU Cortex-R5)
# Peripherals: DRAM (128MB), ARM PrimeCell PL011 UART
# ====================================================================

CROSS_COMPILE ?= arm-none-eabi-
CXX := $(CROSS_COMPILE)g++
CC  := $(CROSS_COMPILE)gcc
AS  := $(CROSS_COMPILE)as

QEMU := qemu-system-arm

# Source file to build (specify via SRC=path/to/test.cpp)
SRC ?=
RUNTIME_SRC := ../runtime/sv_runtime.cpp

ifeq ($(filter clean help,$(MAKECMDGOALS)),)
ifeq ($(strip $(SRC)),)
$(error SRC is not specified. Usage: make [run] SRC=path/to/test.cpp)
endif
endif

TARGET_NAME := $(basename $(notdir $(SRC)))
BUILD_DIR   := build
TARGET_ELF  := $(BUILD_DIR)/$(TARGET_NAME).elf

MCPU := cortex-r4

# Common compilation flags
COMMON_FLAGS := -mcpu=$(MCPU) -mthumb -ffunction-sections -fdata-sections -Wall -Wextra -O2

# C++ specific flags: -fno-exceptions, -fno-rtti, C++17
CXXFLAGS := $(COMMON_FLAGS) -std=c++17 -fno-exceptions -fno-rtti -I../runtime
CFLAGS   := $(COMMON_FLAGS)
ASFLAGS  := -mcpu=$(MCPU)

LDFLAGS  := -T versatilepb.ld -nostartfiles -Wl,--gc-sections

OBJS := $(BUILD_DIR)/startup.o \
        $(BUILD_DIR)/syscalls.o \
        $(BUILD_DIR)/sv_runtime.o \
        $(BUILD_DIR)/$(TARGET_NAME).o

.PHONY: all run clean help

all: $(TARGET_ELF)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/startup.o: startup.s | $(BUILD_DIR)
	$(AS) $(ASFLAGS) $< -o $@

$(BUILD_DIR)/syscalls.o: syscalls.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/sv_runtime.o: $(RUNTIME_SRC) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD_DIR)/$(TARGET_NAME).o: $(SRC) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(TARGET_ELF): $(OBJS) versatilepb.ld
	$(CXX) $(OBJS) $(LDFLAGS) -o $@
	@echo "[QEMU] Successfully built bare-metal ELF: $@"

# Run in QEMU versatilepb
run: $(TARGET_ELF)
	@echo "[QEMU] Launching QEMU (versatilepb / Cortex-R5)..."
	$(QEMU) -M versatilepb -cpu cortex-r5 -m 128M -nographic -semihosting -audio none -kernel $<

clean:
	rm -rf $(BUILD_DIR)

help:
	@echo "Usage:"
	@echo "  make SRC=<path>      - Build bare-metal ELF for the given C++ source"
	@echo "  make run SRC=<path>  - Build and run the ELF in QEMU"
	@echo "  make clean           - Clean build artifacts"

