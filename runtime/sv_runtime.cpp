#include "sv_runtime.hpp"
#include <cstdarg>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <cctype>

/*
 * ====================================================================
 * SystemVerilog Random Number System Tasks ($urandom, $urandom_range, $srandom)
 * ====================================================================
 */
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

/*
 * ====================================================================
 * SystemVerilog String Formatting Tasks & Functions ($sformat, $sformatf)
 * ====================================================================
 */

/* $sformat task implementation */
void sv_sformat(std::string &out_str, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    char buf[1024];
    std::vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    out_str = buf;
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

/*
 * ====================================================================
 * Plusargs System Tasks ($test$plusargs, $value$plusargs)
 * ====================================================================
 */
static std::vector<std::string> s_plusargs;

void sv_clear_plusargs(void) {
    s_plusargs.clear();
}

static void register_single_arg(const std::string &arg) {
    if (arg.empty()) return;
    std::string s = (arg[0] == '+') ? arg.substr(1) : arg;

    size_t eq_pos = s.find('=');
    std::string prefix = (eq_pos != std::string::npos) ? s.substr(0, eq_pos) : s;

    for (auto &existing : s_plusargs) {
        size_t ex_eq = existing.find('=');
        std::string ex_prefix = (ex_eq != std::string::npos) ? existing.substr(0, ex_eq) : existing;
        if (ex_prefix == prefix) {
            existing = s;
            return;
        }
    }
    s_plusargs.push_back(s);
}

void sv_init_plusargs(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        if (argv[i] && argv[i][0] == '+') {
            register_single_arg(argv[i]);
        }
    }
}

void sv_set_plusargs_string(const std::string &cmdline) {
    size_t start = 0;
    while (start < cmdline.size()) {
        while (start < cmdline.size() && (cmdline[start] == ' ' || cmdline[start] == '\t' || cmdline[start] == '\r' || cmdline[start] == '\n')) {
            start++;
        }
        if (start >= cmdline.size()) break;
        size_t end = start;
        while (end < cmdline.size() && cmdline[end] != ' ' && cmdline[end] != '\t' && cmdline[end] != '\r' && cmdline[end] != '\n') {
            end++;
        }
        std::string tok = cmdline.substr(start, end - start);
        if (!tok.empty() && tok[0] == '+') {
            register_single_arg(tok);
        }
        start = end;
    }
}

void sv_set_default_plusarg(const std::string &prefix, const std::string &val) {
    std::string p = (!prefix.empty() && prefix[0] == '+') ? prefix.substr(1) : prefix;
    for (const auto &existing : s_plusargs) {
        if (existing.rfind(p, 0) == 0) {
            return; // already present
        }
    }
    register_single_arg(p + val);
}

int sv_test_plusargs(const std::string &name) {
    std::string p = (!name.empty() && name[0] == '+') ? name.substr(1) : name;
    for (const auto &existing : s_plusargs) {
        if (existing.rfind(p, 0) == 0) {
            return 1;
        }
    }
    return 0;
}

bool sv_find_plusarg_value(const char *fmt, std::string &out_val, std::string &out_spec) {
    if (!fmt) return false;
    const char *p = (fmt[0] == '+') ? (fmt + 1) : fmt;
    const char *pct = std::strchr(p, '%');
    if (!pct) return false;

    std::string prefix(p, pct - p);
    out_spec = (pct + 1);

    for (const auto &arg : s_plusargs) {
        if (arg.rfind(prefix, 0) == 0) {
            out_val = arg.substr(prefix.size());
            return true;
        }
    }
    return false;
}

int sv_value_plusargs(const char *fmt, std::string &out_str) {
    std::string val, spec;
    if (!sv_find_plusarg_value(fmt, val, spec)) {
        return 0;
    }
    out_str = val;
    return 1;
}

int sv_value_plusargs(const char *fmt, char *out_str) {
    std::string val, spec;
    if (!sv_find_plusarg_value(fmt, val, spec)) {
        return 0;
    }
    if (out_str) {
        std::strcpy(out_str, val.c_str());
    }
    return 1;
}

/*
 * ====================================================================
 * Bare-metal Memory File API (C++ Implementation)
 * ====================================================================
 */

#include <memory>

/* Active static asset table */
const MemAssetEntry *g_mem_assets = nullptr;

/* Default maximum write file size */
static size_t s_default_write_size = MAX_MEM_WRITE_FILE_SIZE;

/* Dynamically created and written memory assets */
struct DynamicAsset {
    std::string path;
    std::vector<uint8_t> buffer;
    size_t written_size = 0;
    mutable MemAssetEntry entry_cache = {};
};

static std::vector<std::unique_ptr<DynamicAsset>> s_dynamic_assets;

/* Runtime handle structure for open memory streams */
struct MemFileHandle {
    uint8_t *write_data;        /* Pointer to writable buffer, or nullptr if read-only */
    const uint8_t *read_data;   /* Pointer to readable buffer (static or dynamic) */
    size_t size;                /* Capacity if write mode, or readable size if read mode */
    size_t cursor;              /* Current cursor offset */
    bool in_use;                /* Whether this handle slot is active */
    bool is_write;              /* True if opened in write/append mode */
    DynamicAsset *asset_ref;    /* Reference to underlying dynamic asset */
};

/* Static handle table for open memory streams */
static MemFileHandle s_handles[MAX_MEM_OPEN_FILES] = {};

static MemFileHandle *get_handle(int fd) {
    if (fd < 1 || fd > MAX_MEM_OPEN_FILES) {
        return nullptr;
    }
    MemFileHandle *h = &s_handles[fd - 1];
    return h->in_use ? h : nullptr;
}

void mem_set_assets(const MemAssetEntry *assets) {
    g_mem_assets = assets;
}

void mem_set_default_write_size(size_t size) {
    s_default_write_size = size;
}

size_t mem_get_default_write_size(void) {
    return s_default_write_size;
}

void mem_clear_dynamic_assets(void) {
    s_dynamic_assets.clear();
}

/* Look up an asset entry: checks dynamic written assets first, then static g_mem_assets */
const MemAssetEntry *mem_find_asset(const std::string &path) {
    // 1. Search dynamically created / written assets
    for (auto &da : s_dynamic_assets) {
        if (da && da->path == path) {
            da->entry_cache.path = da->path.c_str();
            da->entry_cache.data = da->buffer.data();
            da->entry_cache.size = da->written_size;
            return &da->entry_cache;
        }
    }
    // 2. Search statically configured g_mem_assets
    if (g_mem_assets) {
        for (int i = 0; g_mem_assets[i].path != nullptr; i++) {
            if (path == g_mem_assets[i].path) {
                return &g_mem_assets[i];
            }
        }
    }
    return nullptr;
}

int mem_fopen(const std::string &path, const std::string &mode) {
    if (path.empty()) {
        return 0;
    }

    bool is_write_mode = (mode.find('w') != std::string::npos);
    bool is_append_mode = (mode.find('a') != std::string::npos);

    if (is_write_mode || is_append_mode) {
        /* Find existing dynamic asset or allocate a new one */
        DynamicAsset *asset = nullptr;
        for (auto &da : s_dynamic_assets) {
            if (da && da->path == path) {
                asset = da.get();
                break;
            }
        }
        if (!asset) {
            auto new_asset = std::make_unique<DynamicAsset>();
            new_asset->path = path;
            asset = new_asset.get();
            s_dynamic_assets.push_back(std::move(new_asset));
        }

        if (is_write_mode) {
            /* Reset/truncate buffer for write mode */
            asset->buffer.assign(s_default_write_size, 0);
            asset->written_size = 0;
        } else {
            /* Append mode: ensure buffer is at least default size */
            if (asset->buffer.size() < s_default_write_size) {
                asset->buffer.resize(s_default_write_size, 0);
            }
        }

        /* Allocate an empty handle slot */
        for (int i = 0; i < MAX_MEM_OPEN_FILES; i++) {
            if (!s_handles[i].in_use) {
                s_handles[i].write_data = asset->buffer.data();
                s_handles[i].read_data = asset->buffer.data();
                s_handles[i].size = asset->buffer.size(); /* Available write capacity */
                s_handles[i].cursor = is_append_mode ? asset->written_size : 0;
                s_handles[i].in_use = true;
                s_handles[i].is_write = true;
                s_handles[i].asset_ref = asset;
                return i + 1; /* Return 1-based fd */
            }
        }
        return 0; /* No available file handle slot */
    }

    /* Read mode: lookup in common asset registry */
    // Check if it's in dynamic assets
    DynamicAsset *dyn_asset = nullptr;
    for (auto &da : s_dynamic_assets) {
        if (da && da->path == path) {
            dyn_asset = da.get();
            break;
        }
    }

    const uint8_t *data_ptr = nullptr;
    size_t data_size = 0;

    if (dyn_asset) {
        data_ptr = dyn_asset->buffer.data();
        data_size = dyn_asset->written_size; /* Only read up to actual written size */
    } else {
        const MemAssetEntry *entry = mem_find_asset(path);
        if (!entry) {
            return 0; /* File not found in assets */
        }
        data_ptr = entry->data;
        data_size = entry->size;
    }

    /* Allocate an empty handle slot for reading */
    for (int i = 0; i < MAX_MEM_OPEN_FILES; i++) {
        if (!s_handles[i].in_use) {
            s_handles[i].write_data = nullptr;
            s_handles[i].read_data = data_ptr;
            s_handles[i].size = data_size;
            s_handles[i].cursor = 0;
            s_handles[i].in_use = true;
            s_handles[i].is_write = false;
            s_handles[i].asset_ref = dyn_asset;
            return i + 1; /* Return 1-based fd */
        }
    }

    return 0; /* No available file handle slot */
}

int mem_fclose(int fd) {
    MemFileHandle *h = get_handle(fd);
    if (!h) {
        return -1;
    }

    if (h->is_write && h->asset_ref) {
        if (h->cursor > h->asset_ref->written_size) {
            h->asset_ref->written_size = h->cursor;
        }
    }

    h->in_use = false;
    h->write_data = nullptr;
    h->read_data = nullptr;
    h->size = 0;
    h->cursor = 0;
    h->is_write = false;
    h->asset_ref = nullptr;
    return 0;
}

int mem_vfprintf(int fd, const char *fmt, va_list args) {
    MemFileHandle *h = get_handle(fd);
    if (!h || !h->is_write || !h->write_data) {
        return -1;
    }
    if (!fmt) {
        return 0;
    }

    char stack_buf[512];
    va_list args_copy;
    va_copy(args_copy, args);
    int needed = std::vsnprintf(stack_buf, sizeof(stack_buf), fmt, args_copy);
    va_end(args_copy);

    if (needed < 0) {
        return -1;
    }

    std::string heap_buf;
    const char *src_str = stack_buf;
    if (static_cast<size_t>(needed) >= sizeof(stack_buf)) {
        heap_buf.resize(needed + 1);
        std::vsnprintf(&heap_buf[0], heap_buf.size(), fmt, args);
        src_str = heap_buf.c_str();
    }

    size_t to_write = static_cast<size_t>(needed);
    if (h->cursor + to_write > h->size) {
        to_write = (h->size > h->cursor) ? (h->size - h->cursor) : 0;
    }

    if (to_write > 0) {
        std::memcpy(h->write_data + h->cursor, src_str, to_write);
        h->cursor += to_write;
        if (h->asset_ref && h->cursor > h->asset_ref->written_size) {
            h->asset_ref->written_size = h->cursor;
        }
    }
    return static_cast<int>(to_write);
}

int mem_fwrite(int fd, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    int ret = mem_vfprintf(fd, fmt, args);
    va_end(args);
    return ret;
}

int mem_fdisplay(int fd, const char *fmt, ...) {
    if (!fmt) {
        return mem_fwrite(fd, "\n");
    }

    char stack_buf[512];
    va_list args;
    va_start(args, fmt);
    va_list args_copy;
    va_copy(args_copy, args);
    int needed = std::vsnprintf(stack_buf, sizeof(stack_buf), fmt, args_copy);
    va_end(args_copy);

    if (needed < 0) {
        va_end(args);
        return -1;
    }

    std::string out_str;
    if (static_cast<size_t>(needed) < sizeof(stack_buf)) {
        out_str.assign(stack_buf, needed);
    } else {
        out_str.resize(needed + 1);
        std::vsnprintf(&out_str[0], out_str.size(), fmt, args);
        out_str.resize(needed);
    }
    va_end(args);

    if (out_str.empty() || out_str.back() != '\n') {
        out_str.push_back('\n');
    }

    MemFileHandle *h = get_handle(fd);
    if (!h || !h->is_write || !h->write_data) {
        return -1;
    }

    size_t to_write = out_str.size();
    if (h->cursor + to_write > h->size) {
        to_write = (h->size > h->cursor) ? (h->size - h->cursor) : 0;
    }

    if (to_write > 0) {
        std::memcpy(h->write_data + h->cursor, out_str.data(), to_write);
        h->cursor += to_write;
        if (h->asset_ref && h->cursor > h->asset_ref->written_size) {
            h->asset_ref->written_size = h->cursor;
        }
    }
    return static_cast<int>(to_write);
}

size_t mem_fread_bytes(void *ptr, size_t total_bytes, int fd) {
    MemFileHandle *h = get_handle(fd);
    if (!h || !ptr || total_bytes == 0) {
        return 0;
    }

    if (h->cursor >= h->size) {
        return 0; /* EOF */
    }

    size_t avail_bytes = h->size - h->cursor;
    size_t copy_bytes = (total_bytes < avail_bytes) ? total_bytes : avail_bytes;

    std::memcpy(ptr, h->read_data + h->cursor, copy_bytes);
    h->cursor += copy_bytes;

    return copy_bytes;
}

int mem_fseek(int fd, long offset, int whence) {
    MemFileHandle *h = get_handle(fd);
    if (!h) {
        return -1;
    }

    long new_pos = 0;
    switch (whence) {
        case SEEK_SET:
            new_pos = offset;
            break;
        case SEEK_CUR:
            new_pos = static_cast<long>(h->cursor) + offset;
            break;
        case SEEK_END:
            new_pos = static_cast<long>(h->size) + offset;
            break;
        default:
            return -1;
    }

    if (new_pos < 0 || static_cast<size_t>(new_pos) > h->size) {
        return -1; /* Out of bounds */
    }

    h->cursor = static_cast<size_t>(new_pos);
    return 0;
}

long mem_ftell(int fd) {
    MemFileHandle *h = get_handle(fd);
    if (!h) {
        return -1;
    }
    return static_cast<long>(h->cursor);
}

int mem_feof(int fd) {
    MemFileHandle *h = get_handle(fd);
    if (!h) {
        return 1;
    }
    return (h->cursor >= h->size) ? 1 : 0;
}

int mem_rewind(int fd) {
    return mem_fseek(fd, 0, SEEK_SET);
}

int mem_ferror(int fd, std::string &str) {
    MemFileHandle *h = get_handle(fd);
    if (!h) {
        str = "Invalid or closed file descriptor";
        return -1;
    }
    str.clear();
    return 0;
}

int mem_fgetc(int fd) {
    MemFileHandle *h = get_handle(fd);
    if (!h || h->cursor >= h->size) {
        return EOF;
    }
    return static_cast<int>(h->read_data[h->cursor++]);
}

int mem_ungetc(int c, int fd) {
    (void)c;
    MemFileHandle *h = get_handle(fd);
    if (!h || h->cursor == 0) {
        return EOF;
    }
    h->cursor--;
    return static_cast<int>(h->read_data[h->cursor]);
}

int mem_fgets(std::string &str, int fd) {
    str.clear();
    MemFileHandle *h = get_handle(fd);
    if (!h || h->cursor >= h->size) {
        return 0; // EOF or invalid fd
    }

    int count = 0;
    while (h->cursor < h->size) {
        char ch = static_cast<char>(h->read_data[h->cursor++]);
        str.push_back(ch);
        count++;
        if (ch == '\n') {
            break;
        }
    }
    return count;
}

int mem_fscanf(int fd, const char *fmt, ...) {
    MemFileHandle *h = get_handle(fd);
    if (!h || !fmt || h->cursor >= h->size) {
        return EOF;
    }

    const char *src_str = reinterpret_cast<const char *>(h->read_data + h->cursor);

    va_list args;
    va_start(args, fmt);
    int matched = vsscanf(src_str, fmt, args);
    va_end(args);

    if (matched <= 0) {
        return matched;
    }

    /* Advance the cursor past the consumed tokens */
    const char *p = src_str;
    const char *end = reinterpret_cast<const char *>(h->read_data + h->size);
    for (int i = 0; i < matched; i++) {
        while (p < end && std::isspace(static_cast<unsigned char>(*p))) p++;
        while (p < end && !std::isspace(static_cast<unsigned char>(*p))) p++;
    }
    int consumed = static_cast<int>(p - src_str);
    if (consumed > 0) {
        h->cursor += static_cast<size_t>(consumed);
        if (h->cursor > h->size) h->cursor = h->size;
    }

    return matched;
}

/* Helper parser for $readmemh / $readmemb */
static int parse_mem_file(const std::string &path, void *mem_array, size_t elem_size, size_t max_elems, int base) {
    if (path.empty() || !mem_array || elem_size == 0 || max_elems == 0) {
        return -1;
    }

    int fd = mem_fopen(path, "r");
    if (!fd) {
        return -1;
    }

    MemFileHandle *h = get_handle(fd);
    if (!h) {
        mem_fclose(fd);
        return -1;
    }

    const char *p = reinterpret_cast<const char *>(h->read_data);
    const char *end = p + h->size;
    size_t write_idx = 0;
    int loaded_count = 0;

    while (p < end && write_idx < max_elems) {
        /* Skip whitespace */
        while (p < end && std::isspace(static_cast<unsigned char>(*p))) p++;
        if (p >= end) break;

        /* Skip single-line comments // */
        if (p + 1 < end && p[0] == '/' && p[1] == '/') {
            p += 2;
            while (p < end && *p != '\n') p++;
            continue;
        }

        /* Skip block comments / * * / */
        if (p + 1 < end && p[0] == '/' && p[1] == '*') {
            p += 2;
            while (p + 1 < end && !(p[0] == '*' && p[1] == '/')) p++;
            if (p + 1 < end) p += 2;
            continue;
        }

        /* Address label (@addr) */
        if (*p == '@') {
            p++;
            char *next_p = nullptr;
            unsigned long addr = std::strtoul(p, &next_p, 16);
            if (next_p > p) {
                write_idx = addr;
                p = next_p;
                continue;
            }
        }

        /* Parse number token */
        char *next_p = nullptr;
        uint64_t val = (base == 2) ? static_cast<uint64_t>(std::strtoull(p, &next_p, 2))
                                   : static_cast<uint64_t>(std::strtoull(p, &next_p, 16));
        if (next_p > p) {
            p = next_p;
            if (write_idx < max_elems) {
                uint8_t *dest = static_cast<uint8_t *>(mem_array) + write_idx * elem_size;
                std::memcpy(dest, &val, elem_size);
                write_idx++;
                loaded_count++;
            }
        } else {
            /* Unknown character, skip 1 */
            p++;
        }
    }

    mem_fclose(fd);
    return loaded_count;
}

int mem_readmemh(const std::string &path, void *mem_array, size_t elem_size, size_t max_elems) {
    return parse_mem_file(path, mem_array, elem_size, max_elems, 16);
}

int mem_readmemb(const std::string &path, void *mem_array, size_t elem_size, size_t max_elems) {
    return parse_mem_file(path, mem_array, elem_size, max_elems, 2);
}
