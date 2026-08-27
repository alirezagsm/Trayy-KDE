function main() {
	console.log("[trayy-kwin] script loaded, connecting to workspace signals");

	workspace.windowAdded.connect(function (window) {
		// Only normal user windows (not desktop, panel, overlay, etc.)
		if (!window.normalWindow) {
			return;
		}

		var rc = window.resourceClass || "";
		var rn = window.resourceName || "";
		var caption = window.caption || "";
		var uuid = window.internalId || window.winId || "";

		console.log(
			`[trayy-kwin] windowAdded: resourceClass="${rc}", resourceName="${rn}", caption="${caption}", winId="${uuid}"`,
		);

		callDBus(
			"org.kde.Trayy",
			"/org/kde/Trayy",
			"org.kde.Trayy.WindowWatcher",
			"WindowAdded",
			rc,
			rn,
			caption,
			String(uuid),
		);

		// KWin doesn't have a workspace-level minimize signal.
		// Attach a per-window listener for minimize events.
		window.minimizedChanged.connect(function () {
			if (window.minimized) {
				var mRc = window.resourceClass || "";
				var mRn = window.resourceName || "";
				var mCaption = window.caption || "";
				var mUuid = window.internalId || window.winId || "";

				console.log(
					`[trayy-kwin] windowMinimized: resourceClass="${mRc}", resourceName="${mRn}", caption="${mCaption}", winId="${mUuid}"`,
				);

				callDBus(
					"org.kde.Trayy",
					"/org/kde/Trayy",
					"org.kde.Trayy.WindowWatcher",
					"WindowMinimized",
					mRc,
					mRn,
					mCaption,
					String(mUuid),
				);
			}
		});
	});

	workspace.windowRemoved.connect(function (window) {
		if (!window.normalWindow) {
			return;
		}

		var rc = window.resourceClass || "";
		var rn = window.resourceName || "";
		var caption = window.caption || "";
		var uuid = window.internalId || window.winId || "";

		console.log(
			`[trayy-kwin] windowRemoved: resourceClass="${rc}", resourceName="${rn}", caption="${caption}", winId="${uuid}"`,
		);

		callDBus(
			"org.kde.Trayy",
			"/org/kde/Trayy",
			"org.kde.Trayy.WindowWatcher",
			"WindowRemoved",
			rc,
			rn,
			caption,
			String(uuid),
		);
	});

	console.log("[trayy-kwin] ready — listening for window events");
}

main();
