use crate::bottom_pane::InputResult;
use crate::slash_command::SlashCommand;

pub(super) fn should_follow_transcript(input_result: &InputResult) -> bool {
    match input_result {
        // Opening settings or archive-except confirmation must preserve the reading anchor.
        // Other inline commands follow so their output, including usage errors, is visible.
        InputResult::Command(
            SlashCommand::Model
            | SlashCommand::Keymap
            | SlashCommand::Memories
            | SlashCommand::Title
            | SlashCommand::Statusline
            | SlashCommand::Theme
            | SlashCommand::ArchiveExcept,
        )
        | InputResult::CommandWithArgs(SlashCommand::ArchiveExcept, ..) => false,
        InputResult::Command(_)
        | InputResult::ServiceTierCommand(_)
        | InputResult::CommandWithArgs(..) => true,
        InputResult::Submitted { .. }
        | InputResult::Queued { .. }
        | InputResult::ParentOwnedInputBlocked
        | InputResult::None => false,
    }
}
