// Fixed settings for the Restaurant OS panels: the venue name, the shelf camera, and the data
// feeds that are planned but not connected. Everything else on the dashboard comes from GET /state.

const BASELINE = {
  venue: "Ravintola Kallio · Helsinki · 72 seats",
  // The camera tabs. `watchesShelf` ties a camera to the sensor events Stockpot really receives,
  // and to the product boxes drawn over its video.
  // `feedUrl` is an MJPEG stream shown in #live-camera-slot. /camera/feed is Stockpot's own relay
  // of the camera named in CAMERA_FEED_URL (.env).
  cameras: [
    { label: "Cam 01 · UNO Q shelf", watchesShelf: true, feedUrl: "/camera/feed" },
  ],
  plannedSources: [
    ["Table reservations", "Booked covers per day, group bookings"],
    ["POS sales", "Dish-level sales to refine usage per dish"],
    ["Expiry dates", "From delivery notes (batch / best-before), reconciled with the camera"],
    ["Weather and local events", "Temperature, concerts, games and holidays, for the walk-in forecast"],
    ["Cold-room temperature sensors", "Cold-chain breaks shorten the effective shelf life"],
  ],
};
