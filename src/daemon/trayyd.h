#pragma once


#include <dbus/dbus.h>
#include <linux/input.h>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <mutex>
#include <vector>

struct AppEntry {
    std::string resource_class;
    std::string title;
};


inline bool trayy_debug_enabled() {
    const char* d = std::getenv("DEBUG");
    return d != nullptr && d[0] != '\0';
}

#define DEBUG_LOG(...) \
    do { \
        if (trayy_debug_enabled()) { \
            fprintf(stderr, __VA_ARGS__); \
            fflush(stderr); \
        } \
    } while (0)


struct WindowInfo {
    std::string resource_class;
    std::string resource_name;
    std::string internal_id;
    std::string caption;
    double x, y, width, height;
    double fx, fy, fwidth, fheight;
    double click_rel_x = 0, click_rel_y = 0;
};

enum class WindowAction {
    None,
    Minimize,
    ToggleAlwaysOnTop,
    SkipTaskbar,
    UnskipTaskbar,
    ActivateWindow,
};


enum class ControlButton {
    None,
    Close,
    Minimize,
    Maximize,
};


struct TopologyDevice {
    int index = -1;
    std::string name;
    std::string phys;
    struct input_id id {};
    bool is_virtual = false;
    std::string sysfs_path;
    std::string devnode;
};


struct ForwardDevice {
    int fd = -1;
};


class TraydService;
class KWinDBus {
public:
    friend class TraydService;
    KWinDBus();
    ~KWinDBus();

    struct ClickInfo {
        int cursor_x = -1;
        int cursor_y = -1;
        WindowInfo window{};
        bool have_window = false;
        double scale_factor = 1.0;
        WindowAction action_taken = WindowAction::None;
    };

    std::optional<ClickInfo> click_info(WindowAction action = WindowAction::None);
    void dispatch();
    double get_scale_factor();
    const std::string& unique_bus_name() const { return unique_bus_name_; }

    bool deploy_window_watcher();
    void on_window_added(const std::string& resource_class, const std::string& window_id);
    void on_window_removed(const std::string& window_id);
    void set_configured_apps(const std::vector<AppEntry>& apps);
    void on_cursor_update(const std::string& raw_data);

private:
    DBusConnection* receive_conn_ = nullptr;
    DBusConnection* kwin_conn_ = nullptr;
    bool name_registered_ = false;

    std::mutex response_mutex_;
    bool response_ready_ = false;
    int cursor_x_ = -1, cursor_y_ = -1;
    WindowInfo window_info_;
    bool have_window_info_ = false;
    WindowAction action_taken_ = WindowAction::None;

    std::string unique_bus_name_;

    std::map<std::string, std::string> app_window_ids_;
    std::vector<AppEntry> configured_apps_;
    std::mutex app_windows_mutex_;

    static DBusHandlerResult filter_func(DBusConnection* conn, DBusMessage* msg, void* user_data);
    void handle_message(DBusConnection* conn, DBusMessage* msg);

    int load_and_run_script(const char* js_content);
    void stop_script(int script_id);
    void unload_script(const std::string& script_name);
    std::string build_script_content(WindowAction action) const;
    std::string build_window_watcher_script() const;
    std::string deploy_script(const std::string& name, const char* content);

    bool wait_for_probe_response(int timeout_ms);
};

ControlButton detect_control_button(const KWinDBus::ClickInfo& click);


std::vector<TopologyDevice> discover_mouse_devices();
std::vector<const TopologyDevice*> select_devices_to_attach(
    const std::vector<TopologyDevice>& all_devices);



int create_forward_device(int source_fd);
void destroy_forward_device(int uinput_fd);
bool forward_event(int uinput_fd, const struct input_event& ev);
int grab_device(int fd);
int ungrab_device(int fd);


bool is_pass_through();
void set_pass_through(bool value);


class TraydService {
public:
    TraydService(KWinDBus& kwin);
    ~TraydService();

    bool init();
    void dispatch();
    void signal_minimize(const std::string& window_id, const std::string& resource_class);

private:
    KWinDBus& kwin_;
    DBusConnection* service_conn_ = nullptr;
    bool registered_ = false;

    static DBusHandlerResult filter_func(DBusConnection* conn, DBusMessage* msg, void* user_data);
    void handle_method_call(DBusConnection* conn, DBusMessage* msg);
    void send_reply(DBusConnection* conn, DBusMessage* msg, DBusMessage* reply);
};