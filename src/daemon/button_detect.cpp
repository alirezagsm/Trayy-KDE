#include "trayyd.h"
#include <linux/input.h>

static const double TITLE_BTN_WIDTH = 23.0; // breeze default
static const double TITLE_BTN_HEIGHT = 23.0 * 1.75; // conservative estimate
static const double TITLE_BTN_PAD = 7.0;
static const double TITLE_BTN_GAP = 1.0;

static bool is_in_title_bar(double click_y, double height, double pad) {
    return (click_y >= pad && click_y < pad + height);
}

ControlButton detect_control_button(const KWinDBus::ClickInfo& click) {
    if (!click.have_window) return ControlButton::None;

    // Sanity check
    if (click.window.fwidth < 100) {
        return ControlButton::None;
    }
    if (click.scale_factor < 0.5 || click.scale_factor > 5.0) {
        return ControlButton::None;
    }

    double scale = click.scale_factor;
    if (scale <= 0) scale = 1.0;
    double width = TITLE_BTN_WIDTH / scale;
    double height = TITLE_BTN_HEIGHT / scale;
    double pad = TITLE_BTN_PAD / scale;
    double gap = TITLE_BTN_GAP / scale;

    double click_y = click.window.click_rel_y;
    if (!is_in_title_bar(click_y, height, pad)) {
        return ControlButton::None;
    }

    double dist_from_right = click.window.fwidth - click.window.click_rel_x;

    // Position i: [i*pad + (i-1)*width + gap,  i*pad + i*width]
    struct ButtonPos {
        int position; // 1=close, 2=maximize, 3=minimize
        ControlButton button;
    };
    static const ButtonPos buttons[] = {
        {1, ControlButton::Close},
        {2, ControlButton::Maximize},
        {3, ControlButton::Minimize}
    };

    for (const auto& b : buttons) {
        double start = b.position * pad + (b.position - 1) * width + gap;
        double end = b.position * pad + b.position * width;
        if (dist_from_right >= start && dist_from_right <= end) {
            return b.button;
        }
    }

    // account for timing drift.
    double controls_width = 3 * (width + gap) + pad;
    if (dist_from_right <= controls_width) {
        return ControlButton::Close;
    }

    return ControlButton::None;
}
