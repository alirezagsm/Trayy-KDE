#include "trayyd.h"
#include <dbus/dbus.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <chrono>

namespace {
    const char* KWIN_SERVICE = "org.kde.KWin";
    const char* SCRIPTING_PATH = "/Scripting";

    const char* TRAYY_DBUS_NAME = "org.kde.trayyd";
}


std::string KWinDBus::build_script_content(WindowAction action) const {
    std::string action_js;
    if (action == WindowAction::Minimize) {
        action_js = "        w.minimized = true;\n";
    }
    else if (action == WindowAction::ToggleAlwaysOnTop) {
        action_js = "        w.keepAbove = !w.keepAbove;\n";
    }
    else if (action == WindowAction::SkipTaskbar) {
        action_js = "        w.skipTaskbar = true;\n";
    }
    else if (action == WindowAction::UnskipTaskbar) {
        action_js = "        w.skipTaskbar = false;\n";
    }
    else if (action == WindowAction::ActivateWindow) {
        action_js = "        workspace.activeWindow = w;\n";
    }

    return std::string(
        "function run() {\n"
        "    var p = workspace.cursorPos;\n"
        "    var cx = Math.round(p.x);\n"
        "    var cy = Math.round(p.y);\n"
        "    var wl = workspace.windowAt(p);\n"
        "    var rc = ''; var rn = ''; var wid = ''; var wx = 0; var wy = 0; var ww = 0; var wh = 0;\n"
        "    var fx = 0; var fy = 0; var fww = 0; var fwh = 0;\n"
        "    var crx = 0; var cry = 0; var cap = '';\n"
        "    if (wl && wl.length > 0) {\n"
        "        var w = wl[0];\n"
        "        rc = w.resourceClass || '';\n"
        "        rn = w.resourceName || '';\n"
        "        wid = w.internalId || '';\n"
        "        cap = w.caption || '';\n"
        "        wx = w.x || 0;\n"
        "        wy = w.y || 0;\n"
        "        ww = w.width || 0;\n"
        "        wh = w.height || 0;\n"
        "        var fg = w.frameGeometry;\n"
        "        if (fg) {\n"
        "            fx = fg.x || 0;\n"
        "            fy = fg.y || 0;\n"
        "            fww = fg.width || 0;\n"
        "            fwh = fg.height || 0;\n"
        "        }\n"
        "        crx = cx - fx;\n"
        "        cry = cy - fy;\n")
        + action_js +
        std::string(
            "    }\n"
            "    var data = 'x:' + cx + ' y:' + cy + ' rc:' + rc + ' rn:' + rn + ' wid:' + wid + ' wx:' + wx + ' wy:' + wy + ' ww:' + ww + ' wh:' + wh + ' fx:' + fx + ' fy:' + fy + ' fww:' + fww + ' fwh:' + fwh + ' crx:' + crx + ' cry:' + cry + ' cap:' + cap;\n"
            "    callDBus('" + unique_bus_name_ + "', '/', '', 'result', data);\n"
            "}\n"
            "run();");
}


KWinDBus::KWinDBus() {
    DBusError err;
    dbus_error_init(&err);

    receive_conn_ = dbus_bus_get(DBUS_BUS_SESSION, &err);
    if (!receive_conn_) {
        fprintf(stderr, "[kwin_dbus] cannot connect to session bus (receive): %s\n", err.message);
        dbus_error_free(&err);
        return;
    }

    const char* unique_name = dbus_bus_get_unique_name(receive_conn_);
    if (unique_name) {
        unique_bus_name_ = unique_name;
    }
    else {
        fprintf(stderr, "[kwin_dbus] cannot get unique bus name\n");
        return;
    }

    int kwinReqResult = dbus_bus_request_name(receive_conn_, TRAYY_DBUS_NAME, 0, &err);
    if (kwinReqResult != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        const char* errMsg = (dbus_error_is_set(&err)) ? err.message : "(no error set)";
        fprintf(stderr, "[kwin_dbus] cannot register name org.kde.trayyd: result=%d, error='%s'\n", kwinReqResult, errMsg);
        dbus_error_free(&err);
    }

    dbus_connection_add_filter(receive_conn_, filter_func, this, nullptr);

    char match_rule[256];
    snprintf(match_rule, sizeof(match_rule),
        "type='method_call',destination='%s'", unique_bus_name_.c_str());
    DBusError add_match_err;
    dbus_error_init(&add_match_err);
    dbus_bus_add_match(receive_conn_, match_rule, &add_match_err);
    if (dbus_error_is_set(&add_match_err)) {
        fprintf(stderr, "[kwin_dbus] failed to add match rule: %s\n", add_match_err.message);
        dbus_error_free(&add_match_err);
    }

    name_registered_ = true;

    dbus_error_init(&err);
    kwin_conn_ = dbus_bus_get(DBUS_BUS_SESSION, &err);
    if (!kwin_conn_) {
        fprintf(stderr, "[kwin_dbus] cannot connect to session bus (kwin): %s\n", err.message);
        dbus_error_free(&err);
        return;
    }

}

KWinDBus::~KWinDBus() {
    if (name_registered_ && receive_conn_) {
        char match_rule[256];
        snprintf(match_rule, sizeof(match_rule),
            "type='method_call',destination='%s'", unique_bus_name_.c_str());
        dbus_bus_remove_match(receive_conn_, match_rule, nullptr);

        dbus_connection_remove_filter(receive_conn_, filter_func, this);
        dbus_bus_release_name(receive_conn_, TRAYY_DBUS_NAME, nullptr);
    }
    if (receive_conn_) {
        dbus_connection_unref(receive_conn_);
    }
    if (kwin_conn_) {
        dbus_connection_unref(kwin_conn_);
    }
}


std::string KWinDBus::deploy_script(const std::string& name, const char* content) {
    std::string path = "/tmp/" + name + "_" + std::to_string(getpid()) + "_" +
        std::to_string(std::hash<std::string>{}(content)) + ".js";
    int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        fprintf(stderr, "[kwin_dbus] cannot create %s\n", path.c_str());
        return "";
    }
    write(fd, content, strlen(content));
    close(fd);
    return path;
}


int KWinDBus::load_and_run_script(const char* js_content) {
    if (!kwin_conn_) {
        fprintf(stderr, "[kwin_dbus] no kwin connection\n");
        return -1;
    }

    std::string script_name = "trayyd_click_" + std::to_string(getpid()) + "_" +
        std::to_string(std::hash<std::string>{}(js_content));
    std::string path = deploy_script(script_name, js_content);
    if (path.empty()) return -1;

    unload_script(script_name);

    DBusError err;
    dbus_error_init(&err);

    DBusMessage* msg = dbus_message_new_method_call(
        KWIN_SERVICE, SCRIPTING_PATH,
        "org.kde.kwin.Scripting", "loadScript");
    if (!msg) {
        fprintf(stderr, "[kwin_dbus] cannot create loadScript message\n");
        unlink(path.c_str());
        return -1;
    }

    const char* c_path = path.c_str();
    const char* c_name = script_name.c_str();
    dbus_message_append_args(msg, DBUS_TYPE_STRING, &c_path, DBUS_TYPE_STRING, &c_name, DBUS_TYPE_INVALID);

    DBusMessage* reply = dbus_connection_send_with_reply_and_block(kwin_conn_, msg, 3000, &err);
    dbus_message_unref(msg);

    if (!reply) {
        fprintf(stderr, "[kwin_dbus] loadScript failed: %s\n", err.message);
        unlink(path.c_str());
        dbus_error_free(&err);
        return -1;
    }

    int script_id = -1;
    DBusMessageIter iter;
    if (dbus_message_iter_init(reply, &iter) &&
        dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_INT32) {
        dbus_message_iter_get_basic(&iter, &script_id);
    }
    dbus_message_unref(reply);
    dbus_error_free(&err);

    if (script_id < 0) {
        fprintf(stderr, "[kwin_dbus] loadScript returned invalid id\n");
        unlink(path.c_str());
        return -1;
    }

    // Run the loaded script
    if (kwin_conn_ && script_id >= 0) {
        std::string script_obj = "/Scripting/Script" + std::to_string(script_id);
        DBusMessage* run_msg = dbus_message_new_method_call(
            KWIN_SERVICE, script_obj.c_str(),
            "org.kde.kwin.Script", "run");
        if (run_msg) {
            dbus_connection_send(kwin_conn_, run_msg, nullptr);
            dbus_connection_flush(kwin_conn_);
            dbus_message_unref(run_msg);
        }
    }

    return script_id;
}


void KWinDBus::unload_script(const std::string& script_name) {
    if (!kwin_conn_) return;

    DBusMessage* msg = dbus_message_new_method_call(
        KWIN_SERVICE, SCRIPTING_PATH,
        "org.kde.kwin.Scripting", "unloadScript");
    if (!msg) return;

    const char* c_name = script_name.c_str();
    dbus_message_append_args(msg, DBUS_TYPE_STRING, &c_name, DBUS_TYPE_INVALID);

    DBusMessage* reply = dbus_connection_send_with_reply_and_block(kwin_conn_, msg, 2000, nullptr);
    dbus_message_unref(msg);

    if (reply) {
        dbus_message_unref(reply);
    }
}


void KWinDBus::stop_script(int script_id) {
    if (!kwin_conn_ || script_id < 0) return;

    std::string script_obj = "/Scripting/Script" + std::to_string(script_id);
    DBusMessage* stop_msg = dbus_message_new_method_call(
        KWIN_SERVICE, script_obj.c_str(),
        "org.kde.kwin.Script", "stop");
    if (stop_msg) {
        dbus_connection_send(kwin_conn_, stop_msg, nullptr);
        dbus_connection_flush(kwin_conn_);
        dbus_message_unref(stop_msg);
    }
}


DBusHandlerResult KWinDBus::filter_func(DBusConnection* conn, DBusMessage* msg, void* user_data) {
    int type = dbus_message_get_type(msg);
    const char* member = dbus_message_get_member(msg);

    if (type == DBUS_MESSAGE_TYPE_METHOD_CALL && member && strcmp(member, "result") == 0) {
        static_cast<KWinDBus*>(user_data)->handle_message(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }

    if (type == DBUS_MESSAGE_TYPE_METHOD_CALL && member) {
        KWinDBus* self = static_cast<KWinDBus*>(user_data);
        if (strcmp(member, "wadd") == 0) {
            DBusMessageIter iter;
            if (dbus_message_iter_init(msg, &iter)) {
                std::string rc, wid;
                int arg_type = dbus_message_iter_get_arg_type(&iter);
                if (arg_type == DBUS_TYPE_STRING) {
                    const char* val = nullptr;
                    dbus_message_iter_get_basic(&iter, &val);
                    rc = val ? val : "";
                }
                dbus_message_iter_next(&iter);
                arg_type = dbus_message_iter_get_arg_type(&iter);
                if (arg_type == DBUS_TYPE_STRING) {
                    const char* val = nullptr;
                    dbus_message_iter_get_basic(&iter, &val);
                    wid = val ? val : "";
                }
                self->on_window_added(rc, wid);
            }
            DBusMessage* reply = dbus_message_new_method_return(msg);
            if (reply) {
                dbus_connection_send(conn, reply, nullptr);
                dbus_message_unref(reply);
            }
            return DBUS_HANDLER_RESULT_HANDLED;
        }

        if (strcmp(member, "wrem") == 0) {
            DBusMessageIter iter;
            if (dbus_message_iter_init(msg, &iter)) {
                std::string wid;
                int arg_type = dbus_message_iter_get_arg_type(&iter);
                if (arg_type == DBUS_TYPE_STRING) {
                    const char* val = nullptr;
                    dbus_message_iter_get_basic(&iter, &val);
                    wid = val ? val : "";
                }
                self->on_window_removed(wid);
            }
            DBusMessage* reply = dbus_message_new_method_return(msg);
            if (reply) {
                dbus_connection_send(conn, reply, nullptr);
                dbus_message_unref(reply);
            }
            return DBUS_HANDLER_RESULT_HANDLED;
        }

        if (strcmp(member, "cursor") == 0) {
            DBusMessageIter iter;
            if (dbus_message_iter_init(msg, &iter)) {
                std::string raw_data;
                int arg_type = dbus_message_iter_get_arg_type(&iter);
                if (arg_type == DBUS_TYPE_STRING) {
                    const char* val = nullptr;
                    dbus_message_iter_get_basic(&iter, &val);
                    raw_data = val ? val : "";
                }
                self->on_cursor_update(raw_data);
            }
            DBusMessage* reply = dbus_message_new_method_return(msg);
            if (reply) {
                dbus_connection_send(conn, reply, nullptr);
                dbus_message_unref(reply);
            }
            return DBUS_HANDLER_RESULT_HANDLED;
        }
    }

    return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
}

void KWinDBus::handle_message(DBusConnection* conn, DBusMessage* msg) {
    std::string raw_data;
    {
        DBusMessageIter iter;
        if (dbus_message_iter_init(msg, &iter)) {
            int arg_type = dbus_message_iter_get_arg_type(&iter);
            if (arg_type == DBUS_TYPE_STRING) {
                const char* data = nullptr;
                dbus_message_iter_get_basic(&iter, &data);
                if (data) {
                    raw_data = data;
                }
            }
        }
    }

    if (raw_data.empty()) {
        DBusMessage* reply = dbus_message_new_method_return(msg);
        if (reply) {
            dbus_connection_send(conn, reply, nullptr);
            dbus_message_unref(reply);
        }
        return;
    }

    bool is_click_info = raw_data.rfind("x:", 0) == 0;

    if (is_click_info) {
        std::lock_guard<std::mutex> lock(response_mutex_);

        cursor_x_ = -1;
        cursor_y_ = -1;
        window_info_ = WindowInfo{};
        have_window_info_ = false;

        // Space-separated key:value pairs. caption is always last and may contain spaces
        std::string cap_key = " cap:";
        auto cap_pos = raw_data.find(cap_key);
        std::string parse_target = (cap_pos != std::string::npos)
            ? raw_data.substr(0, cap_pos)
            : raw_data;
        window_info_.caption = (cap_pos != std::string::npos)
            ? raw_data.substr(cap_pos + cap_key.size())
            : "";

        char buf[1024];
        strncpy(buf, parse_target.c_str(), sizeof(buf) - 1);
        buf[sizeof(buf) - 1] = '\0';

        char* token = strtok(buf, " ");
        while (token != nullptr) {
            char* colon = strchr(token, ':');
            if (colon) {
                *colon = '\0';
                const char* key = token;
                const char* value = colon + 1;

                if (strcmp(key, "x") == 0) {
                    cursor_x_ = atoi(value);
                }
                else if (strcmp(key, "y") == 0) {
                    cursor_y_ = atoi(value);
                }
                else if (strcmp(key, "rc") == 0) {
                    window_info_.resource_class = value;
                    have_window_info_ = true;
                }
                else if (strcmp(key, "rn") == 0) {
                    window_info_.resource_name = value;
                }
                else if (strcmp(key, "wid") == 0) {
                    window_info_.internal_id = value;
                }
                else if (strcmp(key, "wx") == 0) {
                    window_info_.x = atof(value);
                }
                else if (strcmp(key, "wy") == 0) {
                    window_info_.y = atof(value);
                }
                else if (strcmp(key, "ww") == 0) {
                    window_info_.width = atof(value);
                }
                else if (strcmp(key, "wh") == 0) {
                    window_info_.height = atof(value);
                }
                else if (strcmp(key, "fx") == 0) {
                    window_info_.fx = atof(value);
                }
                else if (strcmp(key, "fy") == 0) {
                    window_info_.fy = atof(value);
                }
                else if (strcmp(key, "fww") == 0) {
                    window_info_.fwidth = atof(value);
                }
                else if (strcmp(key, "fwh") == 0) {
                    window_info_.fheight = atof(value);
                }
                else if (strcmp(key, "crx") == 0) {
                    window_info_.click_rel_x = atof(value);
                }
                else if (strcmp(key, "cry") == 0) {
                    window_info_.click_rel_y = atof(value);
                }
            }
            token = strtok(nullptr, " ");
        }

        action_taken_ = WindowAction::None;
        response_ready_ = true;
    }

    DBusMessage* reply = dbus_message_new_method_return(msg);
    if (reply) {
        dbus_connection_send(conn, reply, nullptr);
        dbus_message_unref(reply);
    }
}


bool KWinDBus::wait_for_probe_response(int timeout_ms) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        if (receive_conn_) {
            dbus_connection_read_write_dispatch(receive_conn_, 0);
        }
        if (kwin_conn_) {
            dbus_connection_read_write_dispatch(kwin_conn_, 0);
        }
        {
            std::lock_guard<std::mutex> lock(response_mutex_);
            if (response_ready_) return true;
        }
        usleep(10000);
    }
    return false;
}


std::optional<KWinDBus::ClickInfo> KWinDBus::click_info(WindowAction action) {
    if (!receive_conn_ || !kwin_conn_) {
        fprintf(stderr, "[kwin_dbus] not initialized\n");
        return std::nullopt;
    }

    {
        std::lock_guard<std::mutex> lock(response_mutex_);
        response_ready_ = false;
        cursor_x_ = -1;
        cursor_y_ = -1;
        have_window_info_ = false;
        window_info_ = WindowInfo{};
        action_taken_ = action;
    }

    std::string js_content = build_script_content(action);
    int script_id_to_stop = load_and_run_script(js_content.c_str());
    if (script_id_to_stop < 0) {
        fprintf(stderr, "[kwin_dbus] failed to load/run click script\n");
        return std::nullopt;
    }

    if (!wait_for_probe_response(1000)) {
        if (script_id_to_stop > 0) stop_script(script_id_to_stop);
        fprintf(stderr, "[kwin_dbus] timeout waiting for click info\n");
        return std::nullopt;
    }

    if (script_id_to_stop > 0) {
        stop_script(script_id_to_stop);
    }

    {
        std::lock_guard<std::mutex> lock(response_mutex_);
        if (!response_ready_ || cursor_x_ < 0) {
            fprintf(stderr, "[kwin_dbus] timeout waiting for click info\n");
            return std::nullopt;
        }
    }

    ClickInfo info;
    info.cursor_x = cursor_x_;
    info.cursor_y = cursor_y_;
    if (have_window_info_) {
        info.window = window_info_;
        info.have_window = true;
    }
    info.scale_factor = get_scale_factor();
    info.action_taken = action_taken_;
    return info;
}

void KWinDBus::dispatch() {
    if (receive_conn_) {
        dbus_connection_read_write_dispatch(receive_conn_, 0);
    }
    if (kwin_conn_) {
        dbus_connection_read_write_dispatch(kwin_conn_, 0);
    }
}


double KWinDBus::get_scale_factor() {
    if (!kwin_conn_) return 1.0;

    DBusError err;
    dbus_error_init(&err);

    DBusMessage* msg = dbus_message_new_method_call(
        KWIN_SERVICE, "/KWin",
        "org.kde.KWin", "supportInformation");
    if (!msg) return 1.0;

    DBusMessage* reply = dbus_connection_send_with_reply_and_block(kwin_conn_, msg, 3000, &err);
    dbus_message_unref(msg);

    if (!reply) {
        fprintf(stderr, "[kwin_dbus] supportInformation failed: %s\n", err.message);
        dbus_error_free(&err);
        return 1.0;
    }
    dbus_error_free(&err);

    DBusMessageIter iter;
    double scale = 1.0;
    if (dbus_message_iter_init(reply, &iter) &&
        dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_STRING) {
        const char* data = nullptr;
        dbus_message_iter_get_basic(&iter, &data);

        if (data) {
            const char* scale_key = strstr(data, "Scale: ");
            if (scale_key) {
                scale = atof(scale_key + 7);                  if (scale <= 0) scale = 1.0;
            }
        }
    }
    dbus_message_unref(reply);
    return scale;
}


std::string KWinDBus::build_window_watcher_script() const {
    return std::string(
        "function main() {\n"
        "    workspace.windowAdded.connect(function(w) {\n"
        "        if (!w.normalWindow) return;\n"
        "        var rc = w.resourceClass || '';\n"
        "        var wid = String(w.internalId || w.winId || '');\n"
        "        callDBus('" + unique_bus_name_ + "', '/', '', 'wadd', rc, wid);\n"
        "    });\n"
        "    workspace.windowRemoved.connect(function(w) {\n"
        "        if (!w.normalWindow) return;\n"
        "        var wid = String(w.internalId || w.winId || '');\n"
        "        callDBus('" + unique_bus_name_ + "', '/', '', 'wrem', wid);\n"
        "    });\n"
        "}\n"
        "main();\n");
}

bool KWinDBus::deploy_window_watcher() {
    if (!kwin_conn_) {
        fprintf(stderr, "[kwin_dbus] no kwin connection — cannot deploy watcher\n");
        return false;
    }

    std::string js_content = build_window_watcher_script();
    std::string script_name = "trayyd_watcher_" + std::to_string(getpid());
    std::string path = deploy_script(script_name, js_content.c_str());
    if (path.empty()) {
        fprintf(stderr, "[kwin_dbus] failed to deploy watcher script\n");
        return false;
    }

    unload_script(script_name);

    DBusError err;
    dbus_error_init(&err);

    DBusMessage* msg = dbus_message_new_method_call(
        KWIN_SERVICE, SCRIPTING_PATH,
        "org.kde.kwin.Scripting", "loadScript");
    if (!msg) {
        unlink(path.c_str());
        return false;
    }

    const char* c_path = path.c_str();
    const char* c_name = script_name.c_str();
    dbus_message_append_args(msg, DBUS_TYPE_STRING, &c_path, DBUS_TYPE_STRING, &c_name, DBUS_TYPE_INVALID);

    DBusMessage* reply = dbus_connection_send_with_reply_and_block(kwin_conn_, msg, 3000, &err);
    dbus_message_unref(msg);

    if (!reply) {
        fprintf(stderr, "[kwin_dbus] loadScript (watcher) failed: %s\n", err.message);
        unlink(path.c_str());
        dbus_error_free(&err);
        return false;
    }

    int script_id = -1;
    DBusMessageIter iter;
    if (dbus_message_iter_init(reply, &iter) &&
        dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_INT32) {
        dbus_message_iter_get_basic(&iter, &script_id);
    }
    dbus_message_unref(reply);
    dbus_error_free(&err);

    if (script_id < 0) {
        fprintf(stderr, "[kwin_dbus] watcher loadScript returned invalid id\n");
        unlink(path.c_str());
        return false;
    }

    std::string script_obj = "/Scripting/Script" + std::to_string(script_id);
    DBusMessage* start_msg = dbus_message_new_method_call(
        KWIN_SERVICE, script_obj.c_str(),
        "org.kde.kwin.Script", "start");
    if (start_msg) {
        dbus_connection_send(kwin_conn_, start_msg, nullptr);
        dbus_connection_flush(kwin_conn_);
        dbus_message_unref(start_msg);
    }

    fprintf(stderr, "[kwin_dbus] kwin script deployed (script_id=%d)\n", script_id);
    return true;
}


void KWinDBus::on_window_added(const std::string& resource_class, const std::string& window_id) {
    fprintf(stderr, "[kwin_dbus] windowAdded: rc='%s' wid='%s'\n", resource_class.c_str(), window_id.c_str());

    std::lock_guard<std::mutex> lock(app_windows_mutex_);
    for (const auto& app : configured_apps_) {
        if (resource_class == app.resource_class) {
            app_window_ids_[window_id] = resource_class;
            fprintf(stderr, "[kwin_dbus]   → tracked (configured app: '%s')\n", app.resource_class.c_str());
            return;
        }
        size_t dot = resource_class.find_last_of(".");
        if (dot != std::string::npos && dot + 1 < resource_class.size()) {
            std::string short_name = resource_class.substr(dot + 1);
            if (short_name == app.resource_class) {
                app_window_ids_[window_id] = resource_class;
                fprintf(stderr, "[kwin_dbus]   → tracked (short name match: '%s')\n", short_name.c_str());
                return;
            }
        }
    }
}

void KWinDBus::on_window_removed(const std::string& window_id) {
    fprintf(stderr, "[kwin_dbus] windowRemoved: wid='%s'\n", window_id.c_str());

    std::lock_guard<std::mutex> lock(app_windows_mutex_);
    if (app_window_ids_.erase(window_id)) {
        fprintf(stderr, "[kwin_dbus]   → removed from tracking\n");
    }
}


void KWinDBus::set_configured_apps(const std::vector<AppEntry>& apps) {
    std::lock_guard<std::mutex> lock(app_windows_mutex_);
    configured_apps_ = apps;
}

void KWinDBus::on_cursor_update(const std::string& raw_data) {
    // cursor update received. no consumer reads cached cursor state yet.
    // kept for future use (real-time cursor position tracking, etc.).
    (void)raw_data;
}
