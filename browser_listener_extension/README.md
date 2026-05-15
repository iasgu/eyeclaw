# Eyeclaw Listener Extension

This folder is the browser listener extension for Eyeclaw.

## What it captures

- URL changes and history route changes
- Tab activation and tab update events
- Click, input, change, focus, visibility, and throttled scroll events
- Screenshots for key candidate events such as page loads, clicks, change events, and large scrolls

## Load it in Edge or Chrome

1. Open `edge://extensions` or `chrome://extensions`
2. Turn on `Developer mode`
3. Click `Load unpacked`
4. Select this folder: `browser_listener_extension`

## Default backend

The extension posts batched events to:

`http://127.0.0.1:8010/api/browser-listener/events`

You can change the API base and toggle the listener from the popup.

The Eyeclaw web console can then analyze the latest listener session through:

`POST /api/browser-listener/analyze`

## Notes

- Input values are trimmed and password inputs are ignored.
- Events stay local unless you point the popup at another backend URL.
- This is designed to complement the existing multimodal workflow in Eyeclaw.
