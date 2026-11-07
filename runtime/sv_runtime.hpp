#ifndef SV_RUNTIME_HPP
#define SV_RUNTIME_HPP

#include <cstdint>
#include <cstdbool>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdarg>
#include <cctype>
#include <string>
#include <vector>
#include <array>
#include <algorithm>
#include <iostream>

/* --- SystemVerilog Multidimensional Fixed-Array Container Templates --- */
template<typename T, size_t D1>
struct Array1D {
    T data[D1] = {};
    T& operator[](size_t i) { return data[i]; }
    const T& operator[](size_t i) const { return data[i]; }
    Array1D& operator=(const T (&src)[D1]) { std::memcpy(data, src, sizeof(data)); return *this; }
};

template<typename T, size_t D1, size_t D2>
struct Array2D {
    T data[D1][D2] = {};
    T* operator[](size_t i) { return data[i]; }
    const T* operator[](size_t i) const { return data[i]; }
    Array2D& operator=(const T (&src)[D1][D2]) { std::memcpy(data, src, sizeof(data)); return *this; }
};

template<typename T, size_t D1, size_t D2, size_t D3>
struct Array3D {
    T data[D1][D2][D3] = {};
    auto operator[](size_t i) { return data[i]; }
    const auto operator[](size_t i) const { return data[i]; }
};

/* --- SystemVerilog Random Number System Tasks ($urandom, $urandom_range, $srandom) --- */
uint32_t sv_urandom(void);
uint32_t sv_urandom_range(uint32_t max_val, uint32_t min_val = 0);
void sv_srandom(uint32_t seed);

/* --- SystemVerilog String Formatting System Tasks ($sformat, $sformatf) --- */

/* $sformat: Task form, formats string in-place into output variable */
void sv_sformat(std::string &out_str, const char *fmt, ...);

/* $sformatf: Function form, returns formatted std::string */
std::string sv_sformatf(const char *fmt, ...);

/* --- SystemVerilog Plusargs Runtime API ($test$plusargs, $value$plusargs) --- */
void sv_init_plusargs(int argc, char **argv);
void sv_set_plusargs_string(const std::string &cmdline);
void sv_set_default_plusarg(const std::string &prefix, const std::string &val);
void sv_clear_plusargs(void);
int sv_test_plusargs(const std::string &name);

bool sv_find_plusarg_value(const char *fmt, std::string &out_val, std::string &out_spec);

int sv_value_plusargs(const char *fmt, std::string &out_str);
int sv_value_plusargs(const char *fmt, char *out_str);

template<typename T>
inline int sv_value_plusargs(const char *fmt, T &out_val) {
    std::string val, spec;
    if (!sv_find_plusarg_value(fmt, val, spec)) {
        return 0;
    }
    if (spec == "h" || spec == "x" || spec == "X") {
        out_val = static_cast<T>(std::strtoull(val.c_str(), nullptr, 16));
    } else if (spec == "o") {
        out_val = static_cast<T>(std::strtoull(val.c_str(), nullptr, 8));
    } else if (spec == "b") {
        out_val = static_cast<T>(std::strtoull(val.c_str(), nullptr, 2));
    } else {
        out_val = static_cast<T>(std::strtoll(val.c_str(), nullptr, 10));
    }
    return 1;
}

/*
 * ====================================================================
 * Bare-metal Memory File API (Ported to C++)
 * ====================================================================
 */
#ifndef MEM_FILE_H
#define MEM_FILE_H
#endif

#ifndef MAX_MEM_OPEN_FILES
#define MAX_MEM_OPEN_FILES 8
#endif

#ifndef MAX_MEM_WRITE_FILE_SIZE
#define MAX_MEM_WRITE_FILE_SIZE (64 * 1024)
#endif

#ifndef SEEK_SET
#define SEEK_SET 0
#define SEEK_CUR 1
#define SEEK_END 2
#endif

#ifndef EOF
#define EOF (-1)
#endif

/*
 * Asset entry representing an embedded or memory-mapped file resource (Input or Output).
 * Generated automatically by sv2cpp or configured manually for bare-metal.
 */
struct MemAssetEntry {
    const char *path;           /* Path string to match exactly (e.g., "job/q0.bin", "config.txt") */
    const uint8_t *data;        /* Pointer to data (read-only ROM/Flash, or points to write_buffer) */
    size_t size;                /* Size in bytes (for read-only assets) */
    uint8_t *write_buffer;      /* Static RAM buffer for writing (nullptr for read-only assets) */
    size_t capacity;            /* Buffer capacity in bytes (0 for read-only assets) */
    size_t *written_size;       /* Pointer to actual written byte count in RAM (nullptr for read-only) */
};

inline size_t mem_asset_get_size(const MemAssetEntry *entry) {
    if (!entry) return 0;
    if (entry->written_size) return *(entry->written_size);
    return entry->size;
}

inline bool mem_asset_is_writable(const MemAssetEntry *entry) {
    return (entry && entry->write_buffer != nullptr && entry->capacity > 0);
}

extern const MemAssetEntry *g_mem_assets;

/* Set or override the active static asset table */
void mem_set_assets(const MemAssetEntry *assets);

/* Configure default maximum write file buffer size (allocated on mem_fopen with "w"/"a") */
void mem_set_default_write_size(size_t size);
size_t mem_get_default_write_size(void);

/* Clear all dynamically written/created assets */
void mem_clear_dynamic_assets(void);

/* Find an asset entry (checks dynamically written files first, then static g_mem_assets) */
const MemAssetEntry *mem_find_asset(const std::string &path);

/* Open a memory asset by path. Returns 1-based fd on success, 0 on error. */
int mem_fopen(const std::string &path, const std::string &mode = "r");

/* Close an open memory file descriptor. Returns 0 on success, -1 on error. */
int mem_fclose(int fd);

/* Formatted output to memory stream ($fwrite, $fdisplay) */
int mem_fwrite(int fd, const char *fmt, ...);
int mem_fdisplay(int fd, const char *fmt = nullptr, ...);
int mem_vfprintf(int fd, const char *fmt, va_list args);

/* Internal stream read helper */
size_t mem_fread_bytes(void *ptr, size_t total_bytes, int fd);

/* SystemVerilog $fread: reads data into target variable or array, returns bytes/elements read */
template<typename T>
inline int mem_fread(T &target, int fd) {
    return static_cast<int>(mem_fread_bytes(&target, sizeof(T), fd));
}

template<typename T, size_t N>
inline int mem_fread(T (&target)[N], int fd) {
    return static_cast<int>(mem_fread_bytes(target, sizeof(T) * N, fd) / sizeof(T));
}

template<typename T>
inline int mem_fread(std::vector<T> &target, int fd) {
    if (target.empty()) return 0;
    return static_cast<int>(mem_fread_bytes(target.data(), sizeof(T) * target.size(), fd) / sizeof(T));
}

inline int mem_fread(std::string &target, int fd) {
    if (target.empty()) return 0;
    return static_cast<int>(mem_fread_bytes(&target[0], target.size(), fd));
}

/* Reposition read cursor. Returns 0 on success, -1 on invalid offset/fd. */
int mem_fseek(int fd, long offset, int whence);

/* Return current cursor offset from beginning. Returns -1 on error. */
long mem_ftell(int fd);

/* Return non-zero (1) if cursor reached or exceeded end of file, 0 otherwise. */
int mem_feof(int fd);

/* Reset read cursor to beginning (offset 0). Equivalent to $fseek(fd, 0, 0). Returns 0 on success, -1 on error. */
int mem_rewind(int fd);

/* SystemVerilog $ferror(fd, str): sets error string, returns 0 if valid, non-zero on error. */
int mem_ferror(int fd, std::string &str);

/* Read a single byte. Returns byte value (0-255) as int, or EOF (-1) on end. */
int mem_fgetc(int fd);

/* Push a character back to the stream (moves cursor back 1 byte). */
int mem_ungetc(int c, int fd);

/* SystemVerilog $fgets: reads line into string, returns number of characters read, or 0 on EOF/error. */
int mem_fgets(std::string &str, int fd);

/* Formatted input scan from memory file. Returns count of successfully matched items. */
int mem_fscanf(int fd, const char *fmt, ...);

/* Flush memory stream */
inline int mem_fflush(int fd = 0) { (void)fd; return 0; }

/*
 * Verilog $readmemh / $readmemb Support
 * Reads hex/bin text from an asset file into target memory array.
 * Supports comments and address labels (@addr).
 */
int mem_readmemh(const std::string &path, void *mem_array, size_t elem_size, size_t max_elems);
int mem_readmemb(const std::string &path, void *mem_array, size_t elem_size, size_t max_elems);

#endif /* SV_RUNTIME_HPP */
