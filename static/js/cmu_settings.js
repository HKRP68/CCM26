/*
 * CMU Animation Settings — Phase 6
 * ═══════════════════════════════════════════════════════════════════════════
 * Binds the Animations form to the engine's settings store.
 *
 * The engine owns persistence and the reduced-motion floor; this file is only
 * the control surface. Three things it is careful about:
 *
 *   • It applies on change, not on submit. A motion setting that only takes
 *     effect after a Save tap cannot be previewed, and a player who closes the
 *     screen without tapping Save would lose the choice they just watched work.
 *     Save exists to confirm what has already been stored.
 *   • A system "reduce motion" preference is shown as what it is — a floor the
 *     app will not animate above — rather than silently overriding the radio.
 *     The chosen level stays selected and comes back if the OS setting changes.
 *   • Storage can be unavailable (private mode, blocked site data). The form
 *     says the choice applies for this session instead of claiming it saved.
 *
 * Exposed as window.CmuMotionSettings.
 */
(() => {
  'use strict';

  const FX = window.CMUAnim;

  const LEVEL_COPY = {
    cinematic: 'Full sequences, lighting and particles.',
    standard: 'The same moments, shorter.',
    minimal: 'A brief confirmation beat, no idle motion.',
    off: 'Results appear immediately with no animation.',
  };

  function open() {
    const form = document.getElementById('cmu-motion-form');
    if (!form) return;
    const status = document.getElementById('cmu-motion-status');
    const note = document.getElementById('cmu-motion-system-note');
    const hint = document.getElementById('cmu-motion-level-hint');

    if (!FX) {
      // Without the engine there is nothing to configure: say so plainly
      // rather than leaving a form that silently does nothing.
      form.hidden = true;
      if (status) status.textContent = 'Animation settings are unavailable in this session.';
      return;
    }
    form.hidden = false;

    const paint = () => {
      const settings = FX.settings;
      const effective = FX.level();
      if (form.elements.motion) form.elements.motion.value = settings.level;
      form.elements.sound.checked = settings.sound;
      form.elements.haptics.checked = settings.haptics;
      form.elements.particles.checked = settings.particles;
      form.elements.fastplay.checked = settings.fastPlay;
      if (hint) hint.textContent = LEVEL_COPY[settings.level] || '';
      if (note) {
        const floored = FX.prefersReducedMotion();
        note.hidden = !floored;
        note.textContent = floored
          ? `Your device asks apps to reduce motion, so CMU is running at “${effective}” whatever is chosen here. Your choice is kept and returns when that device setting changes.`
          : '';
      }
      // Particles are pointless at Minimal and Off; say why rather than
      // leaving a control that appears to do nothing.
      const particlesRow = form.querySelector('[data-motion-particles]');
      if (particlesRow) {
        const idle = effective === 'minimal' || effective === 'off';
        particlesRow.classList.toggle('is-muted', idle);
        form.elements.particles.disabled = idle;
      }
    };

    const save = (changedLabel) => {
      const persisted = FX.setAll({
        level: form.elements.motion ? form.elements.motion.value : FX.settings.level,
        sound: form.elements.sound.checked,
        haptics: form.elements.haptics.checked,
        particles: form.elements.particles.checked,
        fastPlay: form.elements.fastplay.checked,
      });
      paint();
      if (status) {
        status.textContent = persisted
          ? `${changedLabel || 'Animation settings'} saved.`
          : `${changedLabel || 'Animation settings'} applied for this session — this device is not storing preferences.`;
      }
      return persisted;
    };

    // Apply as the player changes things, so the next preview reflects it.
    form.onchange = (event) => {
      const field = event.target;
      const label = field?.name === 'motion' ? 'Animation level'
        : field?.name === 'sound' ? 'Sound effects'
        : field?.name === 'haptics' ? 'Haptic feedback'
        : field?.name === 'particles' ? 'Particle effects'
        : field?.name === 'fastplay' ? 'Fast play' : '';
      save(label);
      if (field?.name === 'haptics' && field.checked) FX.haptic('medium');
      // Keep the Appearance form's own haptics checkbox in step: both write
      // the same preference and must not disagree on screen.
      const themeForm = document.getElementById('cmu-theme-form');
      if (themeForm?.elements.haptics) themeForm.elements.haptics.checked = FX.settings.haptics;
    };

    form.onsubmit = (event) => {
      event.preventDefault();
      save();
    };

    const preview = document.getElementById('cmu-motion-preview');
    if (preview) {
      preview.onclick = () => {
        // A sample of exactly what the current settings produce. Nothing is
        // granted, requested or recorded — it is a swatch.
        const host = preview.closest('.screen') || document.body;
        FX.haptic('medium');
        FX.notify('success');
        FX.confetti(48, 'legend', host);
        FX.particles(preview, { count: 22, tier: 'legend' });
        if (status) {
          status.textContent = FX.isOff()
            ? 'Animations are off — results appear immediately.'
            : `Preview played at “${FX.level()}”.`;
        }
      };
    }

    paint();
    // The OS preference can change while the screen is open.
    if (!open._bound) {
      open._bound = true;
      FX.onChange(() => {
        if (document.getElementById('cmu-motion-form')?.hidden === false) paint();
      });
    }
  }

  window.CmuMotionSettings = { open };
})();
