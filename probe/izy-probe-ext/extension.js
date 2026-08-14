import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `
<node>
  <interface name="org.izy.Probe">
    <method name="GetFocused">
      <arg type="s" name="json" direction="out"/>
    </method>
  </interface>
</node>`;

export default class IzyProbeExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/izy/Probe');
        this._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, 'org.izy.Probe',
            Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        if (this._owner) {
            Gio.bus_unown_name(this._owner);
            this._owner = null;
        }
        this._dbus?.unexport();
        this._dbus = null;
    }

    GetFocused() {
        const w = global.display.get_focus_window();
        if (!w)
            return JSON.stringify({focused: false});
        return JSON.stringify({
            focused: true,
            title: w.get_title(),
            wm_class: w.get_wm_class(),
            gtk_app_id: w.get_gtk_application_id(),
            pid: w.get_pid(),
            // true => native Wayland client, false => running through XWayland
            wayland: w.get_client_type() === Meta.WindowClientType.WAYLAND,
        });
    }
}
