"""Weaknesses found by the security review, each fixed and kept here as the behaviour the bot must have.

Each test went in with its fix, so the public repository never describes a weakness still open.
"""

from test_flow import OWNER_A, PREPARER, make_pdf, world  # noqa: F401  (world is a fixture)

from sealwharf.adapters.inbox import Job, parse_command
from sealwharf.application.worker import process_one

# Approval by the owner.

def test_an_answer_sent_before_its_question_was_asked_approves_nothing(world):  # noqa: F811
    """The owner answers one question twice in a row; the second answer must not approve a document seen only afterwards."""
    first = world.send(PREPARER, make_pdf("Alice Example"), filename="harmless.pdf")
    # A second request waits in line: the owner is asked about one document at a time.
    second = world.send(PREPARER, make_pdf("Alice Example", text="Something else entirely"), filename="other.pdf")
    assert [name for _, _, name in world.chat.forwarded] == ["harmless.pdf"]
    # Both answers leave the owner's phone before the bot reads either of them.
    world.inbox.add([Job("answer-1", "", None, OWNER_A, "text", parse_command("1")),
                     Job("answer-2", "", None, OWNER_A, "text", parse_command("ok"))])
    while process_one(world.inbox, world.chat, world.signers, runtime=world.runtime):
        pass
    assert world.state(first) == "sent"
    # The owner had not been shown other.pdf when the second answer was written.
    assert world.state(second) == "awaiting_confirmation"
    assert [name for _, _, name in world.chat.files] == ["harmless.pdf"]
