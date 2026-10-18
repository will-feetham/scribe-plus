/*
 * Scribe Plus launcher: the main executable inside "Scribe Plus.app".
 *
 * macOS ties privacy permissions (Input Monitoring, Accessibility,
 * Microphone) to the program that is running. If the app simply handed
 * over to the Python interpreter, the permissions would belong to Python,
 * not to Scribe Plus. So this small launcher stays the running program and
 * loads Python into itself as a library, the way py2app's launcher does.
 *
 * It runs ~/Library/Application Support/Scribe Plus/app/scribe_plus_mac.py
 * with the Python environment the installer set up next to it:
 *     libpython.dylib  -> the installed Python's shared library
 *     venv/bin/python3 -> the app's virtual environment
 *
 * Build (CI does this):  clang -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
 *                            -o "Scribe Plus" launcher.c
 */
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void fail(const char *msg, const char *detail) {
    fprintf(stderr, "Scribe Plus: %s %s\n", msg, detail ? detail : "");
    char cmd[1024];
    snprintf(cmd, sizeof cmd,
             "/usr/bin/osascript -e 'display alert \"Scribe Plus\" message \"%s\"' "
             ">/dev/null 2>&1", msg);
    system(cmd);
}

int main(int argc, char **argv) {
    char base[PATH_MAX], lib[PATH_MAX], exe[PATH_MAX], app[PATH_MAX], script[PATH_MAX];
    const char *root = getenv("SCRIBE_PLUS_ROOT");
    const char *home = getenv("HOME");
    if (root && *root) {
        snprintf(base, sizeof base, "%s", root);
    } else if (home && *home) {
        snprintf(base, sizeof base, "%s/Library/Application Support/Scribe Plus", home);
    } else {
        fail("Can't find your home folder.", NULL);
        return 1;
    }
    snprintf(lib, sizeof lib, "%s/libpython.dylib", base);
    snprintf(exe, sizeof exe, "%s/venv/bin/python3", base);
    snprintf(app, sizeof app, "%s/app", base);
    snprintf(script, sizeof script, "%s/scribe_plus_mac.py", app);

    if (access(script, R_OK) != 0 || access(lib, R_OK) != 0) {
        fail("Scribe Plus isn't fully installed. Run the install command from "
             "scribeplus.feetham.us again.", base);
        return 1;
    }
    /* Python takes its own path from PYTHONEXECUTABLE (macOS only), which
       makes it find the venv and its packages. */
    setenv("PYTHONEXECUTABLE", exe, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    if (chdir(app) != 0) { /* not fatal */ }

    void *handle = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!handle) {
        fail("Scribe Plus couldn't load Python. Run the install command again.", dlerror());
        return 1;
    }
    int (*py_main)(int, char **) = (int (*)(int, char **))dlsym(handle, "Py_BytesMain");
    if (!py_main) {
        fail("Scribe Plus couldn't start Python. Run the install command again.", dlerror());
        return 1;
    }

    char **args = calloc((size_t)argc + 2, sizeof(char *));
    if (!args) return 1;
    int n = 0;
    args[n++] = exe;
    args[n++] = script;
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "-psn_", 5) == 0) continue;  /* old Finder argument */
        args[n++] = argv[i];
    }
    args[n] = NULL;
    return py_main(n, args);
}
