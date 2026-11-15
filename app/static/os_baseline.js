// Fixed settings for the Restaurant OS panels: the shelf camera.
// Everything else on the dashboard comes from GET /state.

const BASELINE = {
  // The camera tabs. `watchesShelf` ties a camera to the sensor events Ukko really receives,
  // and to the product boxes drawn over its video.
  // `feedUrl` is an MJPEG stream shown in #live-camera-slot. /camera/feed is Ukko's own relay
  // of the camera named in CAMERA_FEED_URL (.env).
  cameras: [
    { label: "Cam 01 · UNO Q shelf", watchesShelf: true, feedUrl: "/camera/feed" },
  ],
};
