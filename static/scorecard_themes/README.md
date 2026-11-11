# Scorecard artwork

The seven full-size WebP files are illustrated background plates created for
this project. They contain no score data, labels, logos or player images.
Header and foreground artwork keep their proportions on cards of any height.

Files ending in `-thumb.webp` show a completed demonstration scorecard in the
admin theme selector. `normal-thumb.webp` demonstrates the stadium broadcast design.
These are previews only; production match images always render live values.

Keep full-size plates at 1536×1024 or higher. The renderer reads them locally
and caches the immutable source images. Replacing artwork needs a process
restart to refresh that source cache. Selecting a theme in the admin panel
does not require a restart.
