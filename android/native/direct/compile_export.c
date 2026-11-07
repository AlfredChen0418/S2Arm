#if defined(__APPLE__) && !defined(_DARWIN_C_SOURCE)
#define _DARWIN_C_SOURCE
#endif
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "compile_export.h"

#if defined(__ANDROID__)
#include <stdbool.h>
#include <sys/system_properties.h>
#include "../debug_gate.h"
#elif defined(S2A_COMPILE_EXPORT_HOST_TEST)
extern int s2a_direct_diag_debug_open(void);
#endif

int s2a_compile_export_allowed(int debug_open, int explicit_export) {
    return debug_open != 0 || explicit_export != 0;
}

int s2a_compile_export_open(void) {
#if defined(__ANDROID__)
    char value[PROP_VALUE_MAX] = {0};
    const int enabled = __system_property_get("debug.s2a.compile_export", value) == 1 && value[0] == '1';
    return s2a_compile_export_allowed(s2a_debug_entries_open(), enabled);
#elif defined(S2A_COMPILE_EXPORT_HOST_TEST)
    return !!s2a_direct_diag_debug_open();
#else
    return 0;
#endif
}

int s2a_compile_export_external_open(void) {
#if defined(__ANDROID__)
    return s2a_compile_export_open() && s2a_debug_entries_open();
#elif defined(S2A_COMPILE_EXPORT_HOST_TEST)
    return s2a_compile_export_open();
#else
    return 0;
#endif
}

#include "sha256.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

int s2a_compile_export_directory(int debug_open, const char *state_dir, const char *debug_dir, char *out, size_t out_size) {
    if (!out || !out_size) return -1;
    out[0] = 0;
    const char *root = debug_open ? debug_dir : state_dir;
    if (!root || root[0] != '/') return -1;
    int n = snprintf(out, out_size, debug_open ? "%s" : "%s/direct-diagnostics", root);
    if (n < 0 || (size_t)n >= out_size) { out[0] = 0; return -1; }
    return 0;
}

int s2a_compile_export_configure(int allowed, int debug_open, const char *state_dir, const char *debug_dir) {
    if (unsetenv("S2A_DIRECT_DIAG_DIR")) return -1;
    if (!allowed) return 0;
    char path[4096];
    if (s2a_compile_export_directory(debug_open, state_dir, debug_dir, path, sizeof path)) return -1;
    return setenv("S2A_DIRECT_DIAG_DIR", path, 1) ? -1 : 0;
}

#define EXPORT_LIMIT_FILES 4096u
#define EXPORT_LIMIT_BYTES (256ull << 20)

static int export_safe_id(const char *id) {
    size_t n = 0;
    if (!id) return 0;
    for (; id[n]; ++n) {
        char c = id[n];
        if (n >= 120 || !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                          (c >= '0' && c <= '9') || c == '-' || c == '_')) return 0;
    }
    return n != 0;
}

// Length-prefix string fields to avoid ambiguous cohort encodings.
static void export_hash_field(s2a_sha256 *hash, const char *value) {
    size_t n = strlen(value);
    unsigned char bytes[8];
    for (unsigned i = 0; i < 8; i++) bytes[i] = (unsigned char)((uint64_t)n >> (8 * i));
    s2a_sha256_update(hash, bytes, sizeof bytes);
    s2a_sha256_update(hash, (const uint8_t *)value, n);
}

int s2a_compile_export_request(const char *root, unsigned long long session,
    unsigned long long generation, unsigned abi, unsigned parser_version,
    const char *closure, const char *profile_hash, const char *request_id,
    const unsigned char identity[32], const void *json, size_t json_bytes) {
    if (!root || root[0] != '/' || !session || !generation || !abi || !parser_version ||
        !closure || !*closure || !profile_hash || !*profile_hash || !export_safe_id(request_id) ||
        !identity || !json || !json_bytes || json_bytes > EXPORT_LIMIT_BYTES) return -1;
    int rootfd = -1, dirfd = -1, locked = 0, tempfd = -1, result = -1;
    DIR *scan = NULL;
    char temp[80] = {0}, final[224], setname[160], cohort[65], identity_hex[65];
    unsigned char digest[32];
    s2a_sha256 hash;
    s2a_sha256_init(&hash);
    char numeric[80];
    snprintf(numeric, sizeof numeric, "%u:%u", abi, parser_version);
    export_hash_field(&hash, numeric);
    export_hash_field(&hash, closure);
    export_hash_field(&hash, profile_hash);
    s2a_sha256_final(&hash, digest);
    s2a_sha256_hex(digest, cohort);
    s2a_sha256_hex(identity, identity_hex);
    if (snprintf(setname, sizeof setname, "requests-%llu-%llu-%s", session, generation, cohort) >= (int)sizeof setname ||
        snprintf(final, sizeof final, "%s-%s.compile-request.json", request_id, identity_hex) >= (int)sizeof final) return -1;
    rootfd = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (rootfd < 0) goto done;
    if (mkdirat(rootfd, setname, 0700) && errno != EEXIST) goto done;
    dirfd = openat(rootfd, setname, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (dirfd < 0) goto done;
    // 锁目录本身，不另建 .lock 文件（11-09 根修）：多个进程在刚建好的目录里同时 O_CREAT 同一个 .lock，macOS APFS 会偶发给其中一个回 ENOENT，
    //   门禁 compile_export_test 12 进程并发发布偶发一个子进程失败（守望 eefdea169；本机 40 路 ×3 轮 11/120 红）。flock 对目录描述符在 Linux / 安卓 / macOS 都有效
    if (flock(dirfd, LOCK_EX)) goto done;
    locked = 1;
    scan = fdopendir(dup(dirfd));
    if (!scan) goto done;
    unsigned files = 0; uint64_t bytes = 0; int duplicate = 0;
    struct dirent *entry;
    char suffix[90];
    snprintf(suffix, sizeof suffix, "-%s.compile-request.json", identity_hex);
    errno = 0;
    while ((entry = readdir(scan))) {
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        struct stat st;
        if (fstatat(dirfd, entry->d_name, &st, AT_SYMLINK_NOFOLLOW) || !S_ISREG(st.st_mode) || st.st_size < 0) goto done;
        if (++files > EXPORT_LIMIT_FILES || (uint64_t)st.st_size > EXPORT_LIMIT_BYTES - bytes) goto done;
        bytes += (uint64_t)st.st_size;
        size_t n = strlen(entry->d_name), sn = strlen(suffix);
        static const char ext[] = ".compile-request.json";
        size_t en = sizeof ext - 1;
        if (n >= en && !strcmp(entry->d_name + n - en, ext)) {
            if (n <= en + 65 || entry->d_name[n - en - 65] != '-') goto done;
            size_t prefix_len = n - en - 65;
            char prefix[121];
            if (prefix_len >= sizeof prefix) goto done;
            memcpy(prefix, entry->d_name, prefix_len); prefix[prefix_len] = 0;
            if (!export_safe_id(prefix)) goto done;
            for (size_t i = n - en - 64; i < n - en; ++i) {
                char c = entry->d_name[i];
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) goto done;
            }
            if (n > sn && !strcmp(entry->d_name + n - sn, suffix)) duplicate = 1;
        }
    }
    if (errno) goto done;
    if (duplicate) { result = 0; goto done; }
    if (files >= EXPORT_LIMIT_FILES || json_bytes > EXPORT_LIMIT_BYTES - bytes) goto done;
    for (unsigned i = 0; i < 128; i++) {
        snprintf(temp, sizeof temp, ".tmp-%ld-%u", (long)getpid(), i);
        tempfd = openat(dirfd, temp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (tempfd >= 0 || errno != EEXIST) break;
    }
    if (tempfd < 0) { temp[0] = 0; goto done; }
    const unsigned char *p = json;
    size_t left = json_bytes;
    while (left) {
        ssize_t n = write(tempfd, p, left);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) goto done;
        p += n; left -= (size_t)n;
    }
    if (close(tempfd)) { tempfd = -1; goto done; }
    tempfd = -1;
    if (linkat(dirfd, temp, dirfd, final, 0)) {
        struct stat existing;
        if (errno != EEXIST || fstatat(dirfd, final, &existing, AT_SYMLINK_NOFOLLOW) ||
            !S_ISREG(existing.st_mode)) goto done;
    }
    result = 0;
done:
    if (tempfd >= 0) close(tempfd);
    if (temp[0] && dirfd >= 0) unlinkat(dirfd, temp, 0);
    if (scan) closedir(scan);
    if (locked) flock(dirfd, LOCK_UN);
    if (dirfd >= 0) close(dirfd);
    if (rootfd >= 0) close(rootfd);
    return result;
}
