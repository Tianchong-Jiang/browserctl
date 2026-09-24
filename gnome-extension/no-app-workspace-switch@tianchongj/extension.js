import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension, InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';

// Mutter switches workspaces from C when an X client requests it (_NET_ACTIVE_WINDOW
// for a window on another workspace, or _NET_CURRENT_DESKTOP). Shell user controls
// (overview, Alt+Tab, dock, keybindings, notifications) switch through these
// methods from JS, so a switch outside them came from an app and is undone.
const SHELL_ENTRY_POINTS = [
    [Meta.Workspace.prototype, ['activate', 'activate_with_focus']],
    [Meta.Window.prototype, ['activate', 'activate_with_workspace']],
    [Shell.App.prototype, ['activate', 'activate_full', 'activate_window', 'launch']],
];

export default class NoAppWorkspaceSwitch extends Extension {
    enable() {
        const ext = this;
        this._shellDepth = 0;
        this._reverting = false;
        this._injections = new InjectionManager();
        for (const [prototype, names] of SHELL_ENTRY_POINTS) {
            for (const name of names) {
                this._injections.overrideMethod(prototype, name, original => function (...args) {
                    ext._shellDepth++;
                    const result = original.apply(this, args);
                    ext._shellDepth--;
                    return result;
                });
            }
        }
        // Blocked switches and their reverts complete without animation, so no
        // intermediate frame shows the other workspace.
        this._injections.overrideMethod(Main.wm._workspaceAnimation, 'animateSwitch', original => function (from, to, direction, onComplete) {
            if (ext._shellDepth === 0 || ext._reverting) {
                onComplete();
                return;
            }
            original.call(this, from, to, direction, onComplete);
        });
        this._switchedId = global.workspace_manager.connect('workspace-switched', (manager, from, to) => {
            if (this._shellDepth > 0)
                return;
            console.log(`${this.uuid}: blocked workspace switch ${from} -> ${to}`);
            this._reverting = true;
            manager.get_workspace_by_index(from).activate(global.display.get_current_time_roundtrip());
            this._reverting = false;
        });
    }

    disable() {
        global.workspace_manager.disconnect(this._switchedId);
        this._injections.clear();
        this._injections = null;
    }
}
