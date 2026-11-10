# Local card templates

Vision looks for `.png`, `.jpg`, `.jpeg`, `.webp`, or `.bmp` files in this folder. Name each file after the card, for example `K9-17 Izuna.png`. Images are resized to the configured template size and compared with the hand-zone crop using OpenCV. Use clean card-face images at a similar crop to the Master Duel card art for better matches.

No image templates are bundled. If this folder is empty, the app still runs and reports that hand recognition is unavailable until templates are added.
