
import json
import random
import logging
import os

from utils.exception_tracker import log_exception

logger = logging.getLogger(__name__)


class CommentaryEngine:
    def __init__(self, data_path=None):
        if data_path is None:
            # Default to data/commentary_pack.json relative to project root
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_path = os.path.join(base_dir, "data", "commentary_pack.json")

        self.data_path = data_path
        self.data = self._load_data()
        self.events = self.data.get("events", {})
        self.narratives = self.data.get("narratives", {})

    def _load_data(self):
        try:
            with open(self.data_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            log_exception(e)
            logger.error(f"Failed to load commentary pack from {self.data_path}: {e}")
            return {"events": {}, "narratives": {}}

    def get_commentary(self, ball_context, match_state):
        """Generate commentary string."""
        # 1. Determine Event Key
        event_key = self._map_context_to_key(ball_context)

        # 2. Get Micro Commentary (Shot description) — tag-filtered
        micro_text = self._select_template(event_key, ball_context)

        # 3. Get Macro Commentary (Narrative)
        macro_text = self._check_narratives(ball_context, match_state)

        # 4. Combine
        if macro_text:
            return f"{micro_text} {macro_text}"
        return micro_text

    def _map_context_to_key(self, context):
        """Map ball context to a JSON key."""
        outcome_type = context.get("type", "").lower()
        runs = context.get("runs", 0)
        is_extra = context.get("is_extra", False)

        if outcome_type == "wicket":
            wkt_type = context.get("wicket_type", "caught").lower()
            return f"wicket_{wkt_type}"

        if is_extra:
            extra_type = context.get("extra_type", "").lower()
            if "wide" in extra_type:
                return "wide"
            if "no" in extra_type:
                return "noball"
            return "dot"

        if runs == 4:
            return "boundary_four"
        if runs == 6:
            return "boundary_six"
        if runs == 0:
            return "dot"
        if runs == 1:
            return "single"
        if runs == 2:
            return "double"
        if runs == 3:
            return "three"

        return "dot"

    # ------------------------------------------------------------------ #
    #  Tag-based template selection
    # ------------------------------------------------------------------ #

    def _get_bowling_tags(self, context):
        """Derive filter tags from the ball context (bowler type etc.)."""
        tags = set()
        # We don't have the full bowler dict here, but match.py enriches
        # outcome with 'bowling_type' before calling us.
        bowling_type = context.get("bowling_type", "").lower()
        if bowling_type in ("fast", "medium", "fast-medium", "medium-fast"):
            tags.add("pace")
        elif bowling_type in ("spin", "off-spin", "leg-spin", "left-arm spin",
                              "off spin", "leg spin", "left arm spin"):
            tags.add("spin")
        return tags

    def _select_template(self, key, context):
        """Select a template for the given key, preferring tag-matched templates."""
        templates = self.events.get(key, [])
        if not templates:
            # Fallback chain
            if "wicket" in key:
                templates = self.events.get("wicket_caught", [])
            elif "boundary" in key:
                templates = self.events.get("boundary_four", [])
            else:
                return context.get("description", "Play continues.")

        if not templates:
            return context.get("description", "Play continues.")

        # --- Tag filtering ---
        bowling_tags = self._get_bowling_tags(context)

        if bowling_tags:
            # Prefer templates whose tags overlap with the bowling context
            matched = [t for t in templates if bowling_tags & set(t.get("tags", []))]
            if matched:
                templates = matched
            # else: no matches, fall through to all templates (better than nothing)

        template_obj = random.choice(templates)
        text = template_obj.get("text", "")

        return text.format(
            batter=context.get("batter", "The batter"),
            bowler=context.get("bowler", "The bowler"),
            runs=context.get("runs", 0),
            team=context.get("batting_team", "The batting side"),
            fielding_team=context.get("bowling_team", "The fielding side"),
        )

    # ------------------------------------------------------------------ #
    #  Narrative triggers (macro commentary)
    # ------------------------------------------------------------------ #

    def _check_narratives(self, context, state):
        """Check for narrative triggers — all 10 categories."""
        triggers = []
        batter = context.get("batter", "The batter")
        bowler = context.get("bowler", "The bowler")
        batting_team = context.get("batting_team", "The batting side")
        bowling_team = context.get("bowling_team", "The fielding side")

        current_over = state.get("current_over", 0)
        innings = state.get("innings", 1)
        runs = context.get("runs", 0)
        batter_runs_before = state.get("batter_runs", 0)
        batter_runs_after = batter_runs_before + (runs if not context.get("batter_out") else 0)
        partnership_before = state.get("partnership_runs", 0)
        partnership_after = partnership_before + runs

        # --- 1. Collapse: 3+ wickets recently ---
        recent_wickets = state.get("recent_wickets_match", 0)
        if context.get("type") == "wicket" and recent_wickets >= 3:
            triggers.extend(self._format_narratives("collapse_wicket",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 2. Milestone 50: threshold crossing (not exact equality) ---
        if batter_runs_before < 50 <= batter_runs_after:
            triggers.extend(self._format_narratives("milestone_50",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 3. Milestone 100: threshold crossing ---
        if batter_runs_before < 100 <= batter_runs_after:
            triggers.extend(self._format_narratives("milestone_100",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 4. Partnership 50: threshold crossing ---
        if partnership_before < 50 <= partnership_after:
            triggers.extend(self._format_narratives("partnership_50",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 5. Maiden over (detected at ball 6 of an over with 0 runs) ---
        over_runs = state.get("current_over_runs", -1)
        current_ball = state.get("current_ball", 0)
        is_maiden = state.get("is_maiden_over", False)
        if is_maiden:
            triggers.extend(self._format_narratives("maiden_over",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 6. Expensive over (15+ runs in the over, at over end) ---
        if over_runs >= 15 and current_ball >= 5:
            triggers.extend(self._format_narratives("expensive_over",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 7. Big over (12-14 runs in the over, at over end) ---
        if 12 <= over_runs < 15 and current_ball >= 5:
            triggers.extend(self._format_narratives("big_over",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 8. Last over drama (format-aware last over, 2nd innings, close match) ---
        # Uses _fmt_last_over from match state (49 for ListA, 19 for T20).
        _last_over = state.get("_fmt_last_over", 19)
        if innings == 2 and current_over == _last_over and current_ball == 0:
            runs_needed = state.get("runs_needed", 999)
            if 1 <= runs_needed <= 20:
                triggers.extend(self._format_narratives("last_over_drama",
                                                         batter=batter, bowler=bowler,
                                                         team=batting_team,
                                                         fielding_team=bowling_team))

        # --- 9. Death overs entry (format-aware death start, first ball) ---
        # Uses _fmt_death_start from match state (40 for ListA, 16 for T20).
        _death_start = state.get("_fmt_death_start", 16)
        if current_over == _death_start and current_ball == 0:
            triggers.extend(self._format_narratives("death_overs",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 10. Powerplay (over 0, first ball only — announce once) ---
        if current_over == 0 and current_ball == 0:
            triggers.extend(self._format_narratives("powerplay",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        # --- 11. High pressure dot (2nd innings, RRR >= 10, dot ball) ---
        if (innings == 2 and runs == 0 and not context.get("is_extra")
                and state.get("required_run_rate", 0) >= 10
                and current_over >= state.get("_fmt_death_start", 16) - 1):
            triggers.extend(self._format_narratives("high_pressure_dot",
                                                     batter=batter, bowler=bowler,
                                                     team=batting_team,
                                                     fielding_team=bowling_team))

        if triggers:
            return random.choice(triggers)
        return None

    def _format_narratives(self, key, **kwargs):
        """Get narrative templates and format them with context."""
        raw = self.narratives.get(key, [])
        if not raw:
            return []
        formatted = []
        for text in raw:
            try:
                formatted.append(text.format(**kwargs))
            except (KeyError, IndexError):
                log_exception(source="backend")
                formatted.append(text)
        return formatted
