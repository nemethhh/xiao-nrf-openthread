/*
 * Copyright (c) 2020 Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: LicenseRef-Nordic-5-Clause
 */

#include <zephyr/kernel.h>
#include <zephyr/devicetree.h>
#include <zephyr/logging/log.h>
#include <openthread.h>
#include <openthread/dataset.h>

LOG_MODULE_REGISTER(thread_cli, CONFIG_OT_COMMAND_LINE_INTERFACE_LOG_LEVEL);

/* The XIAO's RF switch selects the ceramic antenna while rfsw-ctl is driven at boot
 * (regulator-boot-on) and the U.FL connector when it idles; see overlays/antenna-external.overlay.
 */
#if DT_NODE_EXISTS(DT_NODELABEL(rfsw_ctl))
#define ANTENNA (DT_PROP(DT_NODELABEL(rfsw_ctl), regulator_boot_on) ? "internal" : "external")
#else
#define ANTENNA "n/a"
#endif

int main(void)
{
	printk("thread-cli: antenna=%s\n", ANTENNA);

	/* OpenThread is started manually so a fresh node never forms a network from Kconfig
	 * defaults; a node that already has a network (settings survived reboot) rejoins it.
	 */
	openthread_mutex_lock();
	bool commissioned = otDatasetIsCommissioned(openthread_get_default_instance());
	openthread_mutex_unlock();

	if (commissioned) {
		int err = openthread_run();

		LOG_INF("Rejoining stored Thread network: %d", err);
	}

	LOG_INF("OpenThread CLI running. Use 'ot <command>', e.g. 'ot state'.");
	return 0;
}
