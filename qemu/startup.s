/*
 * Bare-metal Startup Routine for ARM Cortex-R4 on QEMU versatilepb
 */
.syntax unified
.cpu cortex-r4
.arm

.global _start
.type _start, %function

.section .text.startup, "ax", %progbits
_start:
    /* Set stack pointer for SVC mode */
    ldr sp, =__stack_top

    /* Zero out .bss */
    ldr r0, =__bss_start__
    ldr r1, =__bss_end__
    mov r2, #0
1:
    cmp r0, r1
    strlt r2, [r0], #4
    blt 1b

    /* Initialize C++ static constructors (__init_array) */
    bl __libc_init_array

    /* Call main(0, NULL) */
    mov r0, #0
    mov r1, #0
    bl main

    /* Call exit(main_result) */
    bl exit

    /* If exit ever returns, enter low-power sleep loop */
hang:
    wfi
    b hang

.size _start, . - _start

