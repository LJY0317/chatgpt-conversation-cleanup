#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char executable[PATH_MAX];
    uint32_t size = (uint32_t)sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) {
        fprintf(stderr, "Unable to locate the portable app launcher.\n");
        return 1;
    }

    char resolved[PATH_MAX];
    if (realpath(executable, resolved) == NULL) {
        perror("realpath");
        return 1;
    }

    const char suffix[] = "/Contents/MacOS/launcher";
    size_t length = strlen(resolved);
    size_t suffix_length = strlen(suffix);
    if (length <= suffix_length || strcmp(resolved + length - suffix_length, suffix) != 0) {
        fprintf(stderr, "Unexpected portable app layout.\n");
        return 1;
    }
    resolved[length - suffix_length] = '\0';

    char tool[PATH_MAX];
    int written = snprintf(
        tool,
        sizeof(tool),
        "%s/Contents/Resources/chatgpt-cleanup",
        resolved
    );
    if (written < 0 || (size_t)written >= sizeof(tool)) {
        fprintf(stderr, "Portable app path is too long.\n");
        return 1;
    }

    const char *script =
        "on run argv\n"
        "set toolPath to item 1 of argv\n"
        "tell application \"Terminal\"\n"
        "set oldIds to get id of windows\n"
        "do script quoted form of toolPath\n"
        "delay 0.1\n"
        "set cleanupWindow to front window\n"
        "set cleanupWindowId to id of cleanupWindow\n"
        "if oldIds contains cleanupWindowId then error \"Terminal reused an existing window\"\n"
        "repeat while busy of selected tab of cleanupWindow\n"
        "delay 0.1\n"
        "end repeat\n"
        "close cleanupWindow\n"
        "end tell\n"
        "end run\n";

    execl(
        "/usr/bin/osascript",
        "osascript",
        "-e",
        script,
        tool,
        (char *)NULL
    );
    perror("osascript");
    return 1;
}
