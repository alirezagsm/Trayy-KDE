#include "trayyd.h"
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <chrono>
#include <sstream>
#include <algorithm>

namespace {
    const char* SERVICE_NAME = "org.trayy.Daemon";
    const char* INTERFACE = "org.trayy.Daemon";
}


TraydService::TraydService(KWinDBus& kwin) : kwin_(kwin) {}

TraydService::~TraydService() {
    if (registered_ && service_conn_) {
        dbus_bus_release_name(service_conn_, SERVICE_NAME, nullptr);
    }
    if (service_conn_) {
        dbus_connection_unref(service_conn_);
    }
}

bool TraydService::init() {
    DBusError err;
    dbus_error_init(&err);

    service_conn_ = dbus_bus_get(DBUS_BUS_SESSION, &err);
    if (!service_conn_) {
        fprintf(stderr, "[trayd_service] cannot connect to session bus: %s\n", err.message);
        dbus_error_free(&err);
        return false;
    }

    int reqResult = dbus_bus_request_name(service_conn_, SERVICE_NAME, 0, &err);
    if (reqResult != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        const char* msg = (err.message != nullptr) ? err.message : "(unknown error)";
        fprintf(stderr, "[trayd_service] cannot register %s: result=%d, error='%s'\n", SERVICE_NAME, reqResult, msg);
        dbus_error_free(&err);
        return false;
    }

    dbus_connection_add_filter(service_conn_, filter_func, this, nullptr);
    registered_ = true;
    return true;
}

void TraydService::dispatch() {
    if (service_conn_) {
        dbus_connection_read_write_dispatch(service_conn_, 0);
    }
}


DBusHandlerResult TraydService::filter_func(DBusConnection* conn, DBusMessage* msg, void* user_data) {
    int type = dbus_message_get_type(msg);
    if (type != DBUS_MESSAGE_TYPE_METHOD_CALL)
        return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;

    const char* iface = dbus_message_get_interface(msg);
    if (!iface || strcmp(iface, INTERFACE) != 0)
        return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;

    static_cast<TraydService*>(user_data)->handle_method_call(conn, msg);
    return DBUS_HANDLER_RESULT_HANDLED;
}

void TraydService::send_reply(DBusConnection* conn, DBusMessage* msg, DBusMessage* reply) {
    dbus_connection_send(conn, reply, nullptr);
    dbus_message_unref(reply);
}


void TraydService::handle_method_call(DBusConnection* conn, DBusMessage* msg) {
    const char* member = dbus_message_get_member(msg);
    if (!member) {
        DBusMessage* err = dbus_message_new_error(msg, DBUS_ERROR_INVALID_ARGS, "missing member");
        if (err) {
            dbus_connection_send(conn, err, nullptr);
            dbus_message_unref(err);
        }
        return;
    }

    if (strcmp(member, "SetPassThrough") == 0) {
        dbus_bool_t enabled = FALSE;
        dbus_message_get_args(msg, nullptr, DBUS_TYPE_BOOLEAN, &enabled, DBUS_TYPE_INVALID);
        bool b = (enabled == TRUE);
        set_pass_through(b);
        DEBUG_LOG("[trayd_service] SetPassThrough(%d) — mode is now %s\n", b, b ? "PASS-THROUGH" : "INTERCEPT");
        DBusMessage* reply = dbus_message_new_method_return(msg);
        if (reply) {
            send_reply(conn, msg, reply);
        }
    }
    else {
        DBusMessage* err = dbus_message_new_error(msg, DBUS_ERROR_UNKNOWN_METHOD, "unknown method");
        if (err) {
            dbus_connection_send(conn, err, nullptr);
            dbus_message_unref(err);
        }
    }
}


void TraydService::signal_minimize(const std::string& window_id, const std::string& resource_class) {
    if (!service_conn_) {
        fprintf(stderr, "[trayd_service] signal_minimize: no connection\n");
        return;
    }

    DBusMessage* sig = dbus_message_new_signal(
        "/org/trayy/Daemon",
        "org.trayy.Daemon",
        "MinimizeWindow");

    if (!sig) {
        fprintf(stderr, "[trayd_service] signal_minimize: cannot create signal\n");
        return;
    }

    dbus_message_append_args(sig,
        DBUS_TYPE_STRING, &window_id,
        DBUS_TYPE_STRING, &resource_class,
        DBUS_TYPE_INVALID);

    dbus_bool_t result = dbus_connection_send(service_conn_, sig, nullptr);

    if (!result) {
        fprintf(stderr, "[trayd_service] signal_minimize: send failed\n");
        dbus_message_unref(sig);
        return;
    }

    dbus_connection_flush(service_conn_);
    dbus_message_unref(sig);

    DEBUG_LOG("[trayd_service] sent MinimizeWindow signal: window_id=%s rc=%s\n",
        window_id.c_str(), resource_class.c_str());
}
