#include "trayyd.h"
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/uinput.h>
#include <cstring>
#include <cstdint>
#include <poll.h>
#include <cstdio>


static int open_uinput_device() {
    int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) {
        fd = open("/dev/input/uinput", O_WRONLY | O_NONBLOCK);
        if (fd < 0) {
            perror("  FAIL: could not open uinput device");
            return -1;
        }
    }
    return fd;
}


int create_forward_device(int source_fd) {
    int fd = open_uinput_device();
    if (fd < 0) return -1;

    char name[128] = {};
    ioctl(source_fd, EVIOCGNAME(sizeof(name) - 1), name);

    struct input_id input_id = {};
    ioctl(source_fd, EVIOCGID, &input_id);

    uint64_t ev_bits = 0;
    ioctl(source_fd, EVIOCGBIT(0, sizeof(ev_bits)), &ev_bits);

    ioctl(fd, UI_SET_EVBIT, EV_SYN);

    if (ev_bits & (1ULL << EV_KEY)) {
        ioctl(fd, UI_SET_EVBIT, EV_KEY);
        uint64_t key_bits[8] = {};          ioctl(source_fd, EVIOCGBIT(EV_KEY, sizeof(key_bits)), key_bits);
        uint16_t code = 0;
        for (size_t w = 0; w < sizeof(key_bits) / sizeof(uint64_t); w++) {
            for (uint64_t i = 0; i < 64; i++, ++code) {
                if (key_bits[w] & (1ull << i)) {
                    ioctl(fd, UI_SET_KEYBIT, code);
                }
            }
        }
    }

    if (ev_bits & (1ULL << EV_REL)) {
        ioctl(fd, UI_SET_EVBIT, EV_REL);
        uint64_t rel_bits[8] = {};
        ioctl(source_fd, EVIOCGBIT(EV_REL, sizeof(rel_bits)), rel_bits);
        uint16_t code = 0;
        for (size_t w = 0; w < sizeof(rel_bits) / sizeof(uint64_t); w++) {
            for (uint64_t i = 0; i < 64; i++, ++code) {
                if (rel_bits[w] & (1ull << i)) {
                    ioctl(fd, UI_SET_RELBIT, code);
                }
            }
        }
    }

    if (ev_bits & (1ULL << EV_ABS)) {
        ioctl(fd, UI_SET_EVBIT, EV_ABS);
        uint64_t abs_bits[8] = {};
        ioctl(source_fd, EVIOCGBIT(EV_ABS, sizeof(abs_bits)), abs_bits);
        uint16_t code = 0;
        for (size_t w = 0; w < sizeof(abs_bits) / sizeof(uint64_t); w++) {
            for (uint64_t i = 0; i < 64; i++, ++code) {
                if (abs_bits[w] & (1ull << i)) {
                    struct input_absinfo absinfo = {};
                    ioctl(source_fd, EVIOCGABS(code), &absinfo);
                    struct uinput_abs_setup abs_setup = {};
                    abs_setup.code = code;
                    abs_setup.absinfo.value = absinfo.value;
                    abs_setup.absinfo.minimum = absinfo.minimum;
                    abs_setup.absinfo.maximum = absinfo.maximum;
                    abs_setup.absinfo.fuzz = absinfo.fuzz;
                    abs_setup.absinfo.flat = absinfo.flat;
                    abs_setup.absinfo.resolution = absinfo.resolution;
                    ioctl(fd, UI_ABS_SETUP, &abs_setup);
                }
            }
        }
    }

    if (ev_bits & (1ULL << EV_REP)) {
        ioctl(fd, UI_SET_EVBIT, EV_REP);
    }

    if (ev_bits & (1ULL << EV_MSC)) {
        ioctl(fd, UI_SET_EVBIT, EV_MSC);
        uint64_t msc_bits[8] = {};
        ioctl(source_fd, EVIOCGBIT(EV_MSC, sizeof(msc_bits)), msc_bits);
        uint16_t code = 0;
        for (size_t w = 0; w < sizeof(msc_bits) / sizeof(uint64_t); w++) {
            for (uint64_t i = 0; i < 64; i++, ++code) {
                if (msc_bits[w] & (1ull << i)) {
                    ioctl(fd, UI_SET_MSCBIT, code);
                }
            }
        }
    }

    struct uinput_setup setup = {};
    strncpy(setup.name, "trayy", sizeof(setup.name) - 1);
    setup.id.bustype = BUS_USB;
    setup.id.vendor = 0x0001;
    setup.id.product = 0x0001;
    setup.id.version = 0x0100;
    ioctl(fd, UI_DEV_SETUP, &setup);

    if (ioctl(fd, UI_DEV_CREATE) < 0) {
        perror("  FAIL: could not create forward device");
        close(fd);
        return -1;
    }

    return fd;
}


void destroy_forward_device(int uinput_fd) {
    if (uinput_fd >= 0) {
        ioctl(uinput_fd, UI_DEV_DESTROY);
        close(uinput_fd);
    }
}


bool forward_event(int uinput_fd, const struct input_event& ev) {
    do {
        ssize_t n = write(uinput_fd, &ev, sizeof(ev));
        if (n >= 0) return (n == sizeof(ev));
    } while (errno == EINTR);
    return false;
}


int grab_device(int fd) {
    if (ioctl(fd, EVIOCGRAB, (void*)1) < 0) {
        perror("  WARN: could not grab device");
        return -1;
    }
    return 0;
}


int ungrab_device(int fd) {
    if (ioctl(fd, EVIOCGRAB, (void*)0) < 0) {
        perror("  WARN: could not ungrab device");
        return -1;
    }
    return 0;
}
