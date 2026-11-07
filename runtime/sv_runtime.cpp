#include "sv_runtime.hpp"
#include <cstdarg>
#include <cstdlib>
#include <cstdio>
#include <cstring>

/* SystemVerilog Random Number Functions */
uint32_t sv_urandom(void) {
    return static_cast<uint32_t>(rand());
}

uint32_t sv_urandom_range(uint32_t max_val, uint32_t min_val) {
    if (min_val > max_val) {
        uint32_t tmp = min_val;
        min_val = max_val;
        max_val = tmp;
    }
    return min_val + (static_cast<uint32_t>(rand()) % (max_val - min_val + 1));
}

void sv_srandom(uint32_t seed) {
    srand(seed);
}

/* $sformat task implementation */
void sv_sformat(std::string &out_str, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    char buf[1024];
    std::vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    out_str = buf;
}

void sv_sformat(char *out_buf, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    std::vsprintf(out_buf, fmt, args);
    va_end(args);
}

/* $sformatf function implementation */
std::string sv_sformatf(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    char buf[1024];
    std::vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    return std::string(buf);
}

/* $value$plusargs std::string overload */
int sv_value_plusargs(const char *fmt, std::string &out_str) {
    char buf[512] = {0};
    int ret = ::sv_value_plusargs(fmt, buf);
    if (ret) {
        out_str = buf;
    }
    return ret;
}

/* mem_fopen std::string overloads */
int mem_fopen(const std::string &path, const char *mode) {
    return ::mem_fopen(path.c_str(), mode);
}

int mem_fopen(const std::string &path, const std::string &mode) {
    return ::mem_fopen(path.c_str(), mode.c_str());
}

int mem_fopen(const char *path, const std::string &mode) {
    return ::mem_fopen(path, mode.c_str());
}
