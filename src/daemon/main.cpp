#include "trayyd.h"
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <atomic>
#include <vector>
#include <set>
#include <regex>
#include <linux/input.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <sys/inotify.h>

static std::atomic<bool> g_shutdown{ false };

static void signal_handler(int sig) {
    (void)sig;
    g_shutdown.store(true);
}

struct GrabbedDevice {
    int fd = -1;
    int fwd_fd = -1;
    int event_id = -1;
    char name[128] = {};
    bool suppressed_press = false;
};

static std::atomic<bool> g_pass_through{ false };

// inotify device monitor

static int g_device_monitor_fd = -1;

static void release_device_monitor() {
    if (g_device_monitor_fd >= 0) {
        close(g_device_monitor_fd);
        g_device_monitor_fd = -1;
    }
}

static void create_device_monitor() {
    release_device_monitor();
    g_device_monitor_fd = inotify_init();
    if (g_device_monitor_fd >= 0) {
        if (inotify_add_watch(g_device_monitor_fd, "/dev/input", IN_CREATE | IN_DELETE) < 0) {
            fprintf(stderr, "[monitor] could not watch /dev/input: %s\n", strerror(errno));
            release_device_monitor();
        }
    }
}

static void drain_device_monitor() {
    if (g_device_monitor_fd < 0) return;
    char buf[256];
    ssize_t n = read(g_device_monitor_fd, buf, sizeof(buf));
    (void)n; // just consume pending events
}

// grab a single device, returns GrabbedDevice or all -1 fds

static GrabbedDevice try_grab_one(const TopologyDevice& topo_dev) {
    GrabbedDevice dev;
    const char* devnode = topo_dev.devnode.c_str();
    int fd = open(devnode, O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
        fprintf(stderr, "[grabbed] cannot open %s: %s — skipping\n", devnode, strerror(errno));
        return dev;
    }

    // Extract event_id from devnode path
    int eid = -1;
    if (sscanf(devnode, "/dev/input/event%d", &eid) != 1) {
        fprintf(stderr, "[grabbed] cannot parse event id from %s\n", devnode);
        close(fd);
        return dev;
    }

    if (grab_device(fd) < 0) {
        fprintf(stderr, "[grabbed] could not grab %s (%s) — skipping\n", devnode, topo_dev.name.c_str());
        close(fd);
        return dev;
    }

    int fwd_fd = create_forward_device(fd);
    if (fwd_fd < 0) {
        fprintf(stderr, "[grabbed] could not create forward device for %s — ungrabbing\n", topo_dev.name.c_str());
        ungrab_device(fd);
        close(fd);
        return dev;
    }

    dev.fd = fd;
    dev.fwd_fd = fwd_fd;
    dev.event_id = eid;
    snprintf(dev.name, sizeof(dev.name), "%s", topo_dev.name.c_str());
    return dev;
}

// rescan: full re-scan of /dev/input, grab new, release gone

static void rescan_devices(std::vector<GrabbedDevice>& devices) {
    fprintf(stderr, "[monitor] device change detected — rescanning\n");

    // Re-create inotify monitor (avoids stale state, like keymapper does)
    create_device_monitor();

    // Save current grabbed event IDs and mark as "disappeared"
    std::set<int> grabbed_ids;
    for (auto& dev : devices) {
        if (dev.fd >= 0) {
            grabbed_ids.insert(dev.event_id);
        }
    }

    // Discover all current mouse devices
    auto all_devices = discover_mouse_devices();
    auto selected = select_devices_to_attach(all_devices);

    // Build set of currently selected event IDs
    std::set<int> selected_ids;
    for (const auto* topo_dev : selected) {
        int eid = -1;
        if (sscanf(topo_dev->devnode.c_str(), "/dev/input/event%d", &eid) == 1) {
            selected_ids.insert(eid);
        }
    }

    // Grab new devices that aren't already grabbed
    for (const auto* topo_dev : selected) {
        int eid = -1;
        if (sscanf(topo_dev->devnode.c_str(), "/dev/input/event%d", &eid) != 1)
            continue;
        if (grabbed_ids.count(eid))
            continue; // already grabbed

        fprintf(stderr, "[monitor] grabbing new device: %s (%s)\n", topo_dev->name.c_str(), topo_dev->devnode.c_str());
        GrabbedDevice dev = try_grab_one(*topo_dev);
        if (dev.fd >= 0) {
            devices.push_back(dev);
            grabbed_ids.insert(eid);
        }
    }

    // Release devices that disappeared from the selected list
    auto it = devices.begin();
    while (it != devices.end()) {
        if (it->fd >= 0 && !selected_ids.count(it->event_id)) {
            fprintf(stderr, "[monitor] releasing disappeared device: %s (event%d)\n", it->name, it->event_id);
            if (it->fwd_fd >= 0) {
                destroy_forward_device(it->fwd_fd);
                it->fwd_fd = -1;
            }
            if (it->fd >= 0) {
                ungrab_device(it->fd);
                close(it->fd);
                it->fd = -1;
            }
        }
        ++it;
    }

    // Remove fully released devices from vector
    devices.erase(
        std::remove_if(devices.begin(), devices.end(),
            [](const GrabbedDevice& d) { return d.fd < 0; }),
        devices.end());

    // Log current state
    if (!devices.empty()) {
        char buf[512] = {};
        for (size_t i = 0; i < devices.size(); i++) {
            if (i > 0) strncat(buf, ", ", sizeof(buf) - strlen(buf) - 1);
            strncat(buf, devices[i].name, sizeof(buf) - strlen(buf) - 1);
        }
        fprintf(stderr, "[monitor] grabbed: %s\n", buf);
    }
}

bool is_pass_through() {
    return g_pass_through.load(std::memory_order_relaxed);
}

void set_pass_through(bool value) {
    g_pass_through.store(value, std::memory_order_relaxed);
    const char* mode = value ? "PASS-THROUGH" : "INTERCEPT";
    DEBUG_LOG("[pass-through] mode: %s\n", mode);
}

static std::vector<GrabbedDevice> open_grabbed_devices() {
    std::vector<GrabbedDevice> devices;

    auto all_devices = discover_mouse_devices();
    if (all_devices.empty()) {
        fprintf(stderr, "[grabbed] no mouse devices discovered\n");
        return devices;
    }

    auto selected = select_devices_to_attach(all_devices);

    for (const auto* topo_dev : selected) {
        GrabbedDevice dev = try_grab_one(*topo_dev);
        if (dev.fd >= 0) {
            devices.push_back(dev);
        }
    }
    return devices;
}


static void close_grabbed_devices(std::vector<GrabbedDevice>& devices) {
    for (auto& dev : devices) {
        if (dev.fwd_fd >= 0) {
            destroy_forward_device(dev.fwd_fd);
            dev.fwd_fd = -1;
        }
        if (dev.fd >= 0) {
            ungrab_device(dev.fd);
            close(dev.fd);
            dev.fd = -1;
        }
    }
}


static bool read_complete_event(int fd, struct input_event* ev) {
    char* buf = reinterpret_cast<char*>(ev);
    size_t remaining = sizeof(*ev);
    while (remaining > 0) {
        ssize_t n = read(fd, buf, remaining);
        if (n == -1) {
            if (errno == EINTR) continue;
            if (errno == EIO) return false;
            return false;
        }
        if (n == 0) return false;
        buf += n;
        remaining -= static_cast<size_t>(n);
    }
    return true;
}

// Mirrors Python's _kwin_name_matches() in trayy_kwin.py.
static bool app_matches_window(const std::string& app_class,
    const std::string& resource_class,
    const std::string& resource_name,
    const std::string& caption,
    const std::string& title_filter) {
    bool classMatched = false;
    if (app_class.rfind("regex:", 0) == 0) {
        std::string pattern = app_class.substr(6);
        try {
            std::regex re(pattern);
            if (std::regex_search(resource_class, re) ||
                std::regex_search(resource_name, re))
                classMatched = true;
        }
        catch (...) {
            // no match
        }
    }
    else {
        if (!resource_class.empty() && resource_class.find(app_class) != std::string::npos ||
            !resource_name.empty() && resource_name.find(app_class) != std::string::npos)
            classMatched = true;
    }
    if (!classMatched) return false;

    if (!title_filter.empty()) {
        if (title_filter.rfind("regex:", 0) == 0) {
            std::string pattern = title_filter.substr(6);
            try {
                std::regex re(pattern);
                if (!std::regex_search(caption, re))
                    return false;
            }
            catch (...) {
                // no match
                return false;
            }
        }
        else {
            // substring mode
            if (caption.find(title_filter) == std::string::npos)
                return false;
        }
    }

    return true;
}

int main(int argc, char* argv[]) {



    std::vector<AppEntry> configured_apps;

    if (argc >= 2 && std::strcmp(argv[1], "--apps") != 0) {

        const char* config_path = argv[1];
        FILE* fp = fopen(config_path, "r");
        if (fp) {
            char line[512];
            int line_num = 0;
            while (fgets(line, sizeof(line), fp)) {
                size_t len = std::strlen(line);
                while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
                    line[--len] = '\0';

                if (len == 0 || line[0] == '#') {
                    line_num++;
                    continue;
                }

                // e NO_TASKBAR / NO_CLOSE directives
                if (line_num < 2) {
                    line_num++;
                    continue;
                }

                {
                    // Extract app class and optional title filter
                    const char* token = line;
                    while (*token != '\0' && *token != ' ' && *token != '\t')
                        token++;
                    size_t app_len = token - line;
                    if (app_len > 0) {
                        AppEntry entry;
                        entry.resource_class = std::string(line, app_len);
                        // Skip whitespace
                        while (*token == ' ' || *token == '\t')
                            token++;
                        if (*token != '\0') {
                            entry.title = std::string(token);
                        }
                        configured_apps.push_back(entry);
                    }
                }
                line_num++;
            }
            fclose(fp);
        }
        else {
            fprintf(stderr, "[grabbed] cannot open config file %s: %s\n", config_path, strerror(errno));
        }
    }

    for (int i = 1; i < argc; i++) {
        if (std::strcmp(argv[i], "--apps") == 0 && i + 1 < argc) {
            const char* list = argv[i + 1];
            const char* end = list + std::strlen(list);
            while (list < end) {
                const char* comma = std::strstr(list, ",");
                if (!comma) comma = end;
                size_t len = comma - list;
                if (len > 0) {
                    AppEntry entry;
                    entry.resource_class = std::string(list, len);
                    configured_apps.push_back(entry);
                }
                list = comma + 1;
            }
        }
    }

    if (configured_apps.empty()) {
        fprintf(stderr, "[grabbed] no apps configured \n");
        return 1;
    }



    struct sigaction sa {};
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    // Initial device scan
    std::vector<GrabbedDevice> devices = open_grabbed_devices();

    // inotify monitor for /dev/input detection
    create_device_monitor();

    KWinDBus kwin;
    kwin.set_configured_apps(configured_apps);

    TraydService trayd_service(kwin);
    if (!trayd_service.init()) {
        fprintf(stderr, "[trayyd] shutting down: another instance is running\n");
        close_grabbed_devices(devices);
        release_device_monitor();
        return 1;
    }

    if (!devices.empty()) {
        char buf[512] = {};
        for (size_t i = 0; i < devices.size(); i++) {
            if (i > 0) strncat(buf, ", ", sizeof(buf) - strlen(buf) - 1);
            strncat(buf, devices[i].name, sizeof(buf) - strlen(buf) - 1);
        }
        fprintf(stderr, "[trayyd] grabbed: %s\n", buf);
    }
    else {
        fprintf(stderr, "[trayyd] no devices yet — waiting for inotify events\n");
    }

    while (!g_shutdown.load()) {

        fd_set read_set;
        FD_ZERO(&read_set);
        int max_fd = 0;
        for (auto& dev : devices) {
            if (dev.fd > max_fd) max_fd = dev.fd;
            FD_SET(dev.fd, &read_set);
        }

        // Include the inotify monitor FD in select()
        if (g_device_monitor_fd >= 0) {
            if (g_device_monitor_fd > max_fd) max_fd = g_device_monitor_fd;
            FD_SET(g_device_monitor_fd, &read_set);
        }

        struct timeval timeout { 0, 100000 };         int result = select(max_fd + 1, &read_set, nullptr, nullptr, &timeout);

        if (result < 0) {
            if (errno == EINTR) continue;
            break;
        }

        // inotify detected a device change
        if (g_device_monitor_fd >= 0 && FD_ISSET(g_device_monitor_fd, &read_set)) {
            drain_device_monitor();
            rescan_devices(devices);
        }

        kwin.dispatch();
        trayd_service.dispatch();


        for (auto& dev : devices) {
            if (!FD_ISSET(dev.fd, &read_set)) continue;

            struct input_event ev;
            while (read_complete_event(dev.fd, &ev)) {

                // Pass-through mode
                if (is_pass_through()) {
                    if (!forward_event(dev.fwd_fd, ev)) {
                        static bool warned = false;
                        if (!warned) {
                            fprintf(stderr, "[pass-through] write to uinput failed on '%s' — events may be lost\n", dev.name);
                            warned = true;
                        }
                    }
                    continue;
                }

                // Normal mode

                if (ev.type == EV_KEY && ev.value == 1 &&
                    (ev.code == BTN_LEFT || ev.code == BTN_RIGHT)) {

                    auto click = kwin.click_info(WindowAction::None);
                    bool is_configured = false;
                    bool is_close_button = false;

                    if (click && click->have_window) {
                        std::string rc = click->window.resource_class;
                        std::string rn = click->window.resource_name;
                        std::string caption = click->window.caption;

                        if (!rc.empty() || !rn.empty()) {
                            for (const auto& app : configured_apps) {
                                if (app_matches_window(app.resource_class, rc, rn, caption, app.title)) {
                                    is_configured = true;
                                    break;
                                }
                            }
                        }

                        if (is_configured) {
                            ControlButton btn = detect_control_button(*click);
                            is_close_button = (btn == ControlButton::Close);
                        }
                    }

                    if (is_configured && is_close_button) {
                        fprintf(stderr, "[trayyd] intercepted close on %s\n",
                            click->window.resource_class.c_str());
                        trayd_service.signal_minimize(
                            click->window.internal_id,
                            click->window.resource_class);
                        dev.suppressed_press = true;
                        // Send SYN_REPORT to terminate the suppressed event batch
                        struct input_event syn = ev;
                        syn.type = EV_SYN;
                        syn.code = SYN_REPORT;
                        syn.value = 0;
                        forward_event(dev.fwd_fd, syn);
                    }
                    else {
                        // Debug
                        if (click && click->have_window) {
                            DEBUG_LOG(
                                "[click] NOT suppressed: rc=%s rn=%s cap='%s' configured=%d closeBtn=%d "
                                "fww=%g fwh=%g crx=%g cry=%g scale=%g\n",
                                click->window.resource_class.c_str(),
                                click->window.resource_name.c_str(),
                                click->window.caption.c_str(),
                                is_configured, is_close_button,
                                click->window.fwidth, click->window.fheight,
                                click->window.click_rel_x, click->window.click_rel_y,
                                click->scale_factor);
                        }
                        else {
                            DEBUG_LOG("[click] NOT suppressed: no window at cursor (click=%p)\n",
                                (void*)click.has_value());
                        }
                        // Forward button press
                        forward_event(dev.fwd_fd, ev);
                    }
                }
                else if (ev.type == EV_KEY && ev.value == 0 &&
                    dev.suppressed_press &&
                    (ev.code == BTN_LEFT || ev.code == BTN_RIGHT)) {

                    DEBUG_LOG("[grabbed] suppressing matching release\n");
                    dev.suppressed_press = false;
                }
                else {
                    // Forward all other events
                    forward_event(dev.fwd_fd, ev);
                }
            }
        }
    }


    close_grabbed_devices(devices);
    release_device_monitor();
    DEBUG_LOG("[grabbed] shutting down\n");
    return 0;
}
