/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Stand-in for the <nvidia/conftest.h> that NVIDIA's nvidia-oot build generates and that the
 * nvidia-l4t-kernel-oot-headers package leaves out. Valid for L4T 39.2 (kernel 6.8.12-tegra) only.
 *
 * It defines only the macros that the camera headers our two drivers include actually test
 * (media/camera_common.h and media/mc_common.h); jetson/build.sh checks that list and stops if
 * NVIDIA's headers start testing anything else. Both change a struct layout shared with
 * tegra-camera.ko, so a wrong value here corrupts kernel memory instead of failing to build.
 * jetson/build.sh proves the result by building TechNexion's unmodified driver with this file
 * and comparing it instruction for instruction with TechNexion's own prebuilt tevs.ko.
 */
#ifndef _CONFTEST_H
#define _CONFTEST_H

/* struct v4l2_async_connection replaced struct v4l2_async_subdev in Linux 6.5 (include/media/v4l2-async.h). */
#define NV_V4L2_ASYNC_CONNECTION_STRUCT_PRESENT

/*
 * tegra_pmc_io_pad_power_enable(struct tegra_pmc *, enum tegra_io_pad) is a Linux 7.0 API that NVIDIA
 * backported into this 6.8 kernel (include/soc/tegra/pmc.h), so struct camera_common_data carries the
 * extra 'struct tegra_pmc *pmc' member.
 */
#define NV_TEGRA_PMC_IO_PAD_POWER_ENABLE_PRESENT

#endif
