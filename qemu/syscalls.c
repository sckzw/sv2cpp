/*
 * Bare-metal System Calls & PL011 UART driver for ARM Cortex-R4 on QEMU versatilepb
 */
#include <sys/stat.h>
#include <sys/types.h>
#include <errno.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* PL011 UART0 on ARM Versatile/PB */
#define PL011_UART0_BASE 0x101f1000
#define UART_DR (*(volatile uint32_t *)(PL011_UART0_BASE + 0x00))
#define UART_FR (*(volatile uint32_t *)(PL011_UART0_BASE + 0x18))
#define UART_FR_TXFF (1 << 5) /* Transmit FIFO full */
#define UART_FR_RXFE (1 << 4) /* Receive FIFO empty */

static void pl011_putc(char c) {
    while (UART_FR & UART_FR_TXFF) {}
    UART_DR = (uint32_t)c;
}

static char pl011_getc(void) {
    while (UART_FR & UART_FR_RXFE) {}
    return (char)(UART_DR & 0xFF);
}

/* Initialization hooks for newlib */
void _init(void) {}
void _fini(void) {}

/* Handle C++ static destruction */
void *__dso_handle = (void *)0;

int __cxa_atexit(void (*f)(void *), void *p, void *d) {
    (void)f; (void)p; (void)d;
    return 0;
}

/* Output to PL011 UART */
__attribute__((used))
int _write(int fd, const char *buf, int len) {
    (void)fd;
    for (int i = 0; i < len; i++) {
        if (buf[i] == '\n') {
            pl011_putc('\r');
        }
        pl011_putc(buf[i]);
    }
    return len;
}

/* Input from PL011 UART */
__attribute__((used))
int _read(int fd, char *buf, int len) {
    (void)fd;
    int count = 0;
    while (count < len) {
        if (UART_FR & UART_FR_RXFE) {
            break;
        }
        buf[count++] = pl011_getc();
    }
    return count;
}

/* Heap memory management (_sbrk) */
extern char _end;
extern char __heap_limit;
static char *s_heap_ptr = &_end;

__attribute__((used))
void *_sbrk(ptrdiff_t incr) {
    char *prev_heap = s_heap_ptr;
    if (s_heap_ptr + incr > &__heap_limit) {
        errno = ENOMEM;
        return (void *)-1;
    }
    s_heap_ptr += incr;
    return (void *)prev_heap;
}

__attribute__((used))
int _close(int fd) {
    (void)fd;
    return -1;
}

__attribute__((used))
int _fstat(int fd, struct stat *st) {
    (void)fd;
    st->st_mode = S_IFCHR;
    return 0;
}

__attribute__((used))
int _isatty(int fd) {
    (void)fd;
    return 1;
}

__attribute__((used))
int _lseek(int fd, int ptr, int dir) {
    (void)fd; (void)ptr; (void)dir;
    return 0;
}

__attribute__((used))
int _getpid(void) {
    return 1;
}

__attribute__((used))
int _kill(int pid, int sig) {
    (void)pid; (void)sig;
    errno = EINVAL;
    return -1;
}

/* Exit: Gracefully terminates QEMU via semihosting SYS_EXIT */
__attribute__((used))
void _exit(int status) {
    register int r0 __asm__("r0") = 0x18; // SYS_EXIT
    register int r1 __asm__("r1") = (status == 0) ? 0x20026 : 0x20027; // ADP_Stopped_ApplicationExit
#if defined(__thumb__)
    __asm__ volatile ("svc 0xab" : : "r"(r0), "r"(r1) : "memory");
#else
    __asm__ volatile ("svc 0x123456" : : "r"(r0), "r"(r1) : "memory");
#endif
    while (1) {
        __asm__ volatile ("wfi");
    }
}

/* Stubs for libc random / arc4random */
__attribute__((used)) int _getentropy(void *ptr, size_t n) { (void)ptr; (void)n; return -1; }
__attribute__((used)) int getentropy(void *ptr, size_t n) { (void)ptr; (void)n; return -1; }

#ifdef __cplusplus
}
#endif

