/*
 * Linker script for ARM Cortex-R4 on QEMU versatilepb
 * Memory Layout:
 *   RAM: 128MB at 0x00000000
 *   Entry offset: 0x00010000 (leaves lower 64KB for zero-page/vectors)
 */

ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x00010000, LENGTH = 128M - 0x00010000
}

SECTIONS
{
    /* Code and constant data */
    .text : {
        KEEP(*(.text.startup))
        *(.text*)
        *(.rodata*)
        *(.glue_7)
        *(.glue_7t)
    } > RAM

    /* C++ Exception tables (stubbed/minimal) */
    .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } > RAM
    .ARM.exidx : {
        PROVIDE(__exidx_start = .);
        *(.ARM.exidx* .gnu.linkonce.armexidx.*)
        PROVIDE(__exidx_end = .);
    } > RAM

    /* C++ Static constructors table (__libc_init_array) */
    .init_array : {
        . = ALIGN(4);
        PROVIDE_HIDDEN (__init_array_start = .);
        KEEP (*(SORT(.init_array.*)))
        KEEP (*(.init_array*))
        PROVIDE_HIDDEN (__init_array_end = .);
    } > RAM

    /* C++ Static destructors table */
    .fini_array : {
        . = ALIGN(4);
        PROVIDE_HIDDEN (__fini_array_start = .);
        KEEP (*(SORT(.fini_array.*)))
        KEEP (*(.fini_array*))
        PROVIDE_HIDDEN (__fini_array_end = .);
    } > RAM

    /* Initialized data */
    .data : {
        . = ALIGN(4);
        *(.data*)
    } > RAM

    /* Uninitialized data (zeroed at boot) */
    .bss (NOLOAD) : {
        . = ALIGN(4);
        PROVIDE(__bss_start__ = .);
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        PROVIDE(__bss_end__ = .);
    } > RAM

    . = ALIGN(8);
    PROVIDE(_end = .);
    PROVIDE(__end__ = .);

    /* Heap memory: 64MB reserved for dynamic memory allocation */
    __heap_limit = _end + 64M;

    /* Stack pointer top: placed at 120MB offset */
    __stack_top = 0x00010000 + 120M;
}

