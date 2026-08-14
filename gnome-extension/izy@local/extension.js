/*
 * Izy Focus Reporter
 *
 * Publishes the focused window on the session bus so the Izy daemon can read
 * it. This exists because GNOME on Wayland exposes no unprivileged route to
 * the active window title (see docs/STEP0-ENVIRONMENT.md) — every alternative
 * was measured and returns nothing.
 *
 * Read-only by construction: it calls getters on the focus window and owns one
 * bus name. It never moves, closes, focuses or modifies a window, and it holds
 * no history — each call reports the current moment and nothing else.
 */

import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.izy.Focus';
const OBJECT_PATH = '/org/izy/Focus';

const IFACE = `
<node>
  <interface name="org.izy.Focus">
    <!-- Returns a JSON object: {focused, title, wm_class, gtk_app_id, pid, wayland}
         or {focused: false} when nothing has focus. JSON keeps the D-Bus
         signature stable as fields are added in later phases. -->
    <method name="GetFocused">
      <arg type="s" name="json" direction="out"/>
    </method>
    <!-- Liveness check, so the daemon can tell "extension missing" from
         "nothing focused" without guessing. -->
    <method name="Ping">
      <arg type="s" name="version" direction="out"/>
    </method>
  </interface>
</node>`;

const VERSION = '1';

export default class IzyFocusExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null, null, null);
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = null;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    Ping() {
        return VERSION;
    }

    GetFocused() {
        let win = null;
        try {
            win = global.display.get_focus_window();
        } catch (e) {
            return JSON.stringify({focused: false, error: String(e)});
        }

        if (!win)
            return JSON.stringify({focused: false});

        // Overrides (menus, tooltips, drag surfaces) are not "what you are
        // working on" and would otherwise churn the activity log.
        if (typeof win.is_override_redirect === 'function' && win.is_override_redirect())
            return JSON.stringify({focused: false});

        return JSON.stringify({
            focused: true,
            title: win.get_title(),
            wm_class: win.get_wm_class(),
            gtk_app_id: win.get_gtk_application_id(),
            pid: win.get_pid(),
            wayland: win.get_client_type() === Meta.WindowClientType.WAYLAND,
        });
    }
}
