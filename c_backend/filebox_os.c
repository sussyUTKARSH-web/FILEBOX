#include <wchar.h>
#include <wctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#ifdef _WIN32
#include <windows.h>
#include <sys/stat.h>
#include <direct.h>
#define STAT _wstat64
#define STAT_T struct __stat64
#define MKDIR(p) _wmkdir(p)
#define RMDIR(p) _wrmdir(p)
#define REMOVE(p) _wremove(p)
#define RENAME(a,b) _wrename(a,b)
#else
#include <sys/stat.h>
#include <unistd.h>
#define STAT stat
#define STAT_T struct stat
#define MKDIR(p) mkdir(p, 0755)
#define RMDIR(p) rmdir(p)
#define REMOVE(p) remove(p)
#define RENAME(a,b) rename(a,b)
#endif

#define FB_PATH_CAP 32768

/* All managed paths are relative to root. Reject absolute paths and traversal. */
static int safe_relative(const wchar_t *relative) {
    if (!relative || !relative[0] || relative[0] == L'/' || relative[0] == L'\\') return 0;
    if (iswalpha(relative[0]) && relative[1] == L':') return 0;
    const wchar_t *p = relative;
    while (*p) {
        while (*p == L'/' || *p == L'\\') ++p;
        const wchar_t *start = p;
        while (*p && *p != L'/' && *p != L'\\') ++p;
        size_t n = (size_t)(p - start);
        if ((n == 2 && start[0] == L'.' && start[1] == L'.') || (n == 1 && start[0] == L'.')) return 0;
    }
    return 1;
}

static int join_path(const wchar_t *root, const wchar_t *relative, wchar_t *out, size_t cap) {
    if (!root || !safe_relative(relative)) return 0;
    size_t n = wcslen(root);
    if (n + wcslen(relative) + 3 >= cap) return 0;
    wcscpy(out, root);
    if (n && out[n - 1] != L'/' && out[n - 1] != L'\\') out[n++] = L'\\';
    for (size_t i = 0; relative[i]; ++i) out[n++] = (relative[i] == L'/') ? L'\\' : relative[i];
    out[n] = L'\0';
    return 1;
}

static int ensure_parent_dirs(const wchar_t *path) {
    wchar_t tmp[FB_PATH_CAP];
    size_t n = wcslen(path);
    if (n >= FB_PATH_CAP) return 0;
    wcscpy(tmp, path);
    for (size_t i = 0; i < n; ++i) {
        if (tmp[i] == L'/' || tmp[i] == L'\\') {
            if (i == 2 && tmp[1] == L':') continue;
            wchar_t save = tmp[i]; tmp[i] = L'\0';
            if (tmp[0] && MKDIR(tmp) != 0 && errno != EEXIST) {
#ifdef _WIN32
                DWORD e = GetLastError(); if (e != ERROR_ALREADY_EXISTS) { tmp[i] = save; return 0; }
#else
                tmp[i] = save; return 0;
#endif
            }
            tmp[i] = save;
        }
    }
    return 1;
}

static int copy_bytes(const wchar_t *source, const wchar_t *destination) {
    FILE *in = _wfopen(source, L"rb");
    if (!in) return 0;
    FILE *out = _wfopen(destination, L"wb");
    if (!out) { fclose(in); return 0; }
    char buffer[65536]; size_t got; int ok = 1;
    while ((got = fread(buffer, 1, sizeof(buffer), in)) > 0) {
        if (fwrite(buffer, 1, got, out) != got) { ok = 0; break; }
    }
    if (ferror(in)) ok = 0;
    if (fclose(in) != 0) ok = 0;
    if (fclose(out) != 0) ok = 0;
    if (!ok) REMOVE(destination);
    return ok;
}

int fb_make_directory(const wchar_t *root, const wchar_t *relative) {
    wchar_t path[FB_PATH_CAP];
    if (!join_path(root, relative, path, FB_PATH_CAP)) return 0;
    if (!ensure_parent_dirs(path)) return 0;
    if (MKDIR(path) == 0) return 1;
#ifdef _WIN32
    return GetLastError() == ERROR_ALREADY_EXISTS;
#else
    return errno == EEXIST;
#endif
}

int fb_import_file(const wchar_t *root, const wchar_t *source, const wchar_t *relative_destination) {
    wchar_t destination[FB_PATH_CAP];
    if (!source || !join_path(root, relative_destination, destination, FB_PATH_CAP)) return 0;
    if (!ensure_parent_dirs(destination)) return 0;
    return copy_bytes(source, destination);
}

int fb_export_file(const wchar_t *root, const wchar_t *relative_source, const wchar_t *destination) {
    wchar_t source[FB_PATH_CAP];
    if (!destination || !join_path(root, relative_source, source, FB_PATH_CAP)) return 0;
    if (!ensure_parent_dirs(destination)) return 0;
    return copy_bytes(source, destination);
}

int fb_move_file(const wchar_t *root, const wchar_t *old_relative, const wchar_t *new_relative) {
    wchar_t old_path[FB_PATH_CAP], new_path[FB_PATH_CAP];
    if (!join_path(root, old_relative, old_path, FB_PATH_CAP) || !join_path(root, new_relative, new_path, FB_PATH_CAP)) return 0;
    if (!ensure_parent_dirs(new_path)) return 0;
    return RENAME(old_path, new_path) == 0;
}

int fb_delete_file(const wchar_t *root, const wchar_t *relative) {
    wchar_t path[FB_PATH_CAP];
    if (!join_path(root, relative, path, FB_PATH_CAP)) return 0;
    return REMOVE(path) == 0;
}

int fb_delete_empty_directory(const wchar_t *root, const wchar_t *relative) {
    wchar_t path[FB_PATH_CAP];
    if (!join_path(root, relative, path, FB_PATH_CAP)) return 0;
    return RMDIR(path) == 0;
}

int fb_file_size(const wchar_t *root, const wchar_t *relative, int64_t *size_out) {
    wchar_t path[FB_PATH_CAP]; STAT_T info;
    if (!size_out || !join_path(root, relative, path, FB_PATH_CAP) || STAT(path, &info) != 0) return 0;
    *size_out = (int64_t)info.st_size;
    return 1;
}

#ifdef _WIN32
/* The helper receives UTF-8 arguments encoded as ASCII hex so Python and a
   32-bit MinGW build can safely exchange paths with Unicode characters. */
static int hex_digit(char ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

static wchar_t *decode_utf8_hex(const char *hex) {
    size_t length = strlen(hex);
    if ((length & 1) != 0) return NULL;
    size_t byte_count = length / 2;
    char *bytes = (char *)malloc(byte_count + 1);
    if (!bytes) return NULL;
    for (size_t i = 0; i < byte_count; ++i) {
        int high = hex_digit(hex[i * 2]);
        int low = hex_digit(hex[i * 2 + 1]);
        if (high < 0 || low < 0) { free(bytes); return NULL; }
        bytes[i] = (char)((high << 4) | low);
    }
    bytes[byte_count] = '\0';
    int wide_count = MultiByteToWideChar(CP_UTF8, 0, bytes, (int)byte_count, NULL, 0);
    if (wide_count <= 0) { free(bytes); return NULL; }
    wchar_t *wide = (wchar_t *)calloc((size_t)wide_count + 1, sizeof(wchar_t));
    if (!wide) { free(bytes); return NULL; }
    if (MultiByteToWideChar(CP_UTF8, 0, bytes, (int)byte_count, wide, wide_count) <= 0) {
        free(bytes); free(wide); return NULL;
    }
    free(bytes);
    return wide;
}

int main(int argc, char **argv) {
    if (argc < 2) return 2;
    wchar_t *args[4] = {NULL, NULL, NULL, NULL};
    int count = argc - 2;
    if (count < 2 || count > 3) return 2;
    for (int i = 0; i < count; ++i) {
        args[i] = decode_utf8_hex(argv[i + 2]);
        if (!args[i]) { for (int j = 0; j < i; ++j) free(args[j]); return 2; }
    }

    int ok = 0;
    if (strcmp(argv[1], "mkdir") == 0 && count == 2) {
        ok = fb_make_directory(args[0], args[1]);
    } else if (strcmp(argv[1], "import") == 0 && count == 3) {
        ok = fb_import_file(args[0], args[1], args[2]);
    } else if (strcmp(argv[1], "export") == 0 && count == 3) {
        ok = fb_export_file(args[0], args[1], args[2]);
    } else if (strcmp(argv[1], "move") == 0 && count == 3) {
        ok = fb_move_file(args[0], args[1], args[2]);
    } else if (strcmp(argv[1], "delete") == 0 && count == 2) {
        ok = fb_delete_file(args[0], args[1]);
    } else if (strcmp(argv[1], "rmdir") == 0 && count == 2) {
        ok = fb_delete_empty_directory(args[0], args[1]);
    } else if (strcmp(argv[1], "size") == 0 && count == 2) {
        int64_t size = 0;
        ok = fb_file_size(args[0], args[1], &size);
        if (ok) printf("%lld\n", (long long)size);
    }

    for (int i = 0; i < count; ++i) free(args[i]);
    return ok ? 0 : 1;
}
#endif
