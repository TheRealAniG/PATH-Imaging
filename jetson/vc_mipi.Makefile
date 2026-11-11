# Kbuild file for Vision Components' driver, built out of tree by jetson/build.sh.
# Module names and split are VC's own (their patch to nvidia-oot/drivers/media/i2c/Makefile).
# PATH_STOCK_TEGRACAM: see jetson/patches/vc-mipi-stock-tegracam.patch.
ccflags-y += -DPATH_STOCK_TEGRACAM

obj-m += vc_mipi_core.o
vc_mipi_core-objs := vc_mipi/vc_mipi_core.o vc_mipi/vc_mipi_modules.o

obj-m += vc_mipi_camera.o
vc_mipi_camera-objs := vc_mipi/vc_mipi_camera.o
