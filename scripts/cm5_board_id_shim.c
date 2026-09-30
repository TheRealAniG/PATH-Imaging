/*
 * LD_PRELOAD shim: VizionSDK only accepts the Raspberry Pi 5 board ID, so on a CM5
 * it finds no camera. This makes reads of the device-tree board ID return the file
 * named by $VX_FAKE_COMPATIBLE instead. Every other path is untouched.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *redirect(const char *path)
{
	const char *fake = getenv("VX_FAKE_COMPATIBLE");

	if (fake && path && strcmp(path, "/sys/firmware/devicetree/base/compatible") == 0)
		return fake;
	return path;
}

#define WRAP_OPEN(name, ...)						\
	int name(__VA_ARGS__, int flags, ...)				\
	{								\
		static int (*real)();					\
		va_list ap;						\
		int mode;						\
		va_start(ap, flags);					\
		mode = va_arg(ap, int);					\
		va_end(ap);						\
		if (!real)						\
			real = dlsym(RTLD_NEXT, #name);

WRAP_OPEN(open, const char *path) return real(redirect(path), flags, mode); }
WRAP_OPEN(open64, const char *path) return real(redirect(path), flags, mode); }
WRAP_OPEN(openat, int dirfd, const char *path) return real(dirfd, redirect(path), flags, mode); }
WRAP_OPEN(openat64, int dirfd, const char *path) return real(dirfd, redirect(path), flags, mode); }

FILE *fopen(const char *path, const char *mode)
{
	static FILE *(*real)(const char *, const char *);

	if (!real)
		real = dlsym(RTLD_NEXT, "fopen");
	return real(redirect(path), mode);
}

FILE *fopen64(const char *path, const char *mode)
{
	static FILE *(*real)(const char *, const char *);

	if (!real)
		real = dlsym(RTLD_NEXT, "fopen64");
	return real(redirect(path), mode);
}
