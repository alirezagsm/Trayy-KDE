#include "trayyd.h"
#include <algorithm>
#include <cstdint>
#include <dirent.h>
#include <fcntl.h>
#include <map>
#include <set>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

static std::string read_phys_path(int event_index) {
    char path[256];
    snprintf(path, sizeof(path), "/sys/class/input/event%d/device/phys", event_index);

    FILE* f = fopen(path, "r");
    if (!f) return "";

    char buf[128] = {};
    if (fgets(buf, sizeof(buf), f)) {
        size_t len = strlen(buf);
        while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r')) {
            buf[--len] = '\0';
        }
    }
    fclose(f);
    return std::string(buf);
}


static std::string read_device_name_from_sysfs(int event_index) {
    char path[256];
    snprintf(path, sizeof(path), "/sys/class/input/event%d/device/name", event_index);

    FILE* f = fopen(path, "r");
    if (!f) return "";

    char buf[128] = {};
    if (fgets(buf, sizeof(buf), f)) {
        size_t len = strlen(buf);
        while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r')) {
            buf[--len] = '\0';
        }
    }
    fclose(f);
    return std::string(buf);
}

std::vector<TopologyDevice> discover_mouse_devices() {
    std::vector<TopologyDevice> devices;

    DIR* cls = opendir("/sys/class/input");
    if (!cls) {
        fprintf(stderr, "[topology] cannot open /sys/class/input\n");
        return devices;
    }

    std::set<int> event_indices;
    struct dirent* entry = nullptr;
    while ((entry = readdir(cls)) != nullptr) {
        const char* name = entry->d_name;
        if (strncmp(name, "event", 5) == 0) {
            int idx = atoi(name + 5);
            if (idx >= 0 && idx <= 31) {
                event_indices.insert(idx);
            }
        }
    }
    closedir(cls);

    for (int idx : event_indices) {
        char devnode[64];
        snprintf(devnode, sizeof(devnode), "/dev/input/event%d", idx);

        int fd = open(devnode, O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;

        uint64_t rel_bits = 0;
        if (ioctl(fd, EVIOCGBIT(EV_REL, sizeof(rel_bits)), &rel_bits) < 0) {
            close(fd);
            continue;
        }
        if (!((rel_bits & (1ULL << REL_X)) && (rel_bits & (1ULL << REL_Y)))) {
            close(fd);
            continue;
        }

        TopologyDevice dev;
        dev.index = idx;
        dev.sysfs_path = "/sys/class/input/event" + std::to_string(idx);
        dev.devnode = std::string(devnode);

        char name[128] = {};
        if (ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) > 0) {
            dev.name = std::string(name);
        }
        else {
            dev.name = read_device_name_from_sysfs(idx);
            if (dev.name.empty()) {
                dev.name = "event" + std::to_string(idx);
            }
        }

        struct input_id input_id = {};
        if (ioctl(fd, EVIOCGID, &input_id) >= 0) {
            dev.id = input_id;
        }

        close(fd);

        dev.phys = read_phys_path(idx);
        dev.is_virtual = dev.phys.empty();

        devices.push_back(dev);
    }

    std::sort(devices.begin(), devices.end(),
        [](const TopologyDevice& a, const TopologyDevice& b) {
            return a.index < b.index;
        });

    return devices;
}

std::vector<const TopologyDevice*> select_devices_to_attach(
    const std::vector<TopologyDevice>& all_devices) {

    std::map<uint32_t, std::vector<const TopologyDevice*>> groups;

    for (const auto& dev : all_devices) {
        if (dev.name == "trayy") {
            continue;
        }

        uint32_t key = ((uint32_t)dev.id.vendor << 16) | (uint32_t)dev.id.product;
        groups[key].push_back(&dev);
    }

    std::vector<const TopologyDevice*> selected;

    for (auto& [key, devs] : groups) {
        if (devs.empty()) continue;

        const TopologyDevice* best_physical = nullptr;
        const TopologyDevice* best_virtual = nullptr;

        for (const auto* dev : devs) {
            if (dev->is_virtual) {
                if (!best_virtual) {
                    best_virtual = dev;
                }
            }
            else {
                if (!best_physical) {
                    best_physical = dev;
                }
            }
        }

        if (best_virtual) {
            selected.push_back(best_virtual);
        }
        else if (best_physical) {
            selected.push_back(best_physical);
        }
    }

    std::sort(selected.begin(), selected.end(),
        [](const TopologyDevice* a, const TopologyDevice* b) {
            return a->index < b->index;
        });

    return selected;
}
