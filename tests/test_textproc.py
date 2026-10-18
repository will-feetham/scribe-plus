from scribe_plus import textproc as T


def test_parse_dictionary_terms_and_replacements():
    d = T.parse_dictionary("# comment\nButterflyMX\n\ncube control -> kubectl\n  GrowFlow  \nbad ->\n")
    assert d.terms == ["ButterflyMX", "GrowFlow"]
    assert d.replacements == [("cube control", "kubectl")]
    assert d.spellings == ["ButterflyMX", "GrowFlow", "kubectl"]


def test_parse_snippets_with_line_breaks():
    s = T.parse_snippets("# x\ninsert my address = 1 Main St\\nNew Orleans\nnope\n = empty\n")
    assert s == [T.Snippet("insert my address", "1 Main St\nNew Orleans")]


def test_replacements_ignore_case_and_word_boundaries():
    d = T.Dictionary(replacements=[("cube control", "kubectl"), ("grow flow", "GrowFlow")])
    assert T.apply_replacements("Run Cube Control get pods at grow-flow.", d) == "Run kubectl get pods at GrowFlow."
    assert T.apply_replacements("cubecontrol stays", d) == "cubecontrol stays"


def test_snippet_whole_and_inline():
    sn = [T.Snippet("insert my calendar link", "https://cal.com/will")]
    assert T.expand_snippets("Insert my calendar link.", sn) == "https://cal.com/will"
    out = T.expand_snippets("Sure, book here: insert my calendar link. Thanks!", sn)
    assert out == "Sure, book here: https://cal.com/will Thanks!"
    assert T.expand_snippets("nothing here", sn) == "nothing here"


def test_snippet_text_with_backslashes_is_literal():
    sn = [T.Snippet("insert path", r"C:\Dev\tools")]
    assert T.expand_snippets("go to insert path now", sn) == r"go to C:\Dev\tools now"


def test_whisper_prompt_is_bounded():
    d = T.Dictionary(terms=[f"Term{i:03d}" for i in range(200)])
    p = T.whisper_prompt(d, max_chars=120)
    assert p.startswith("Vocabulary: ") and p.endswith(".") and len(p) <= 120
    assert T.whisper_prompt(T.Dictionary()) == ""


def test_cleanup_messages_carry_style_glossary_and_triggers():
    d = T.Dictionary(terms=["ButterflyMX"], replacements=[("grow flow", "GrowFlow")])
    system, msgs = T.build_cleanup_messages(
        "um hello", style_key="email", style_text="An email.", app="outlook.exe",
        dictionary=d, snippets=[T.Snippet("insert my link", "x")])
    assert "an email (outlook.exe)" in system
    assert "Destination style: An email." in system
    assert "ButterflyMX" in system and "GrowFlow" in system and '"grow flow" means "GrowFlow"' in system
    assert '"insert my link"' in system
    assert msgs[-1] == {"role": "user", "content": "<transcript>um hello</transcript>"}
    assert len(msgs) == 2 * len(T.CLEANUP_EXAMPLES) + 1


def test_command_messages():
    system, msgs = T.build_command_messages("hi there", "make it formal", style_key="chat",
                                            style_text="Chat.", app="slack.exe")
    assert "a chat app (slack.exe)" in system
    assert msgs == [{"role": "user", "content":
                     "<selection>hi there</selection>\n<instruction>make it formal</instruction>"}]


def test_postprocess_strips_wrappers():
    assert T.postprocess_output("Here is the cleaned text:\nHello there.") == "Hello there."
    assert T.postprocess_output('"Hello there."', "hello there") == "Hello there."
    assert T.postprocess_output('"quoted"', '"quoted"') == '"quoted"'
    assert T.postprocess_output("<transcript>Hi.</transcript>") == "Hi."
    assert T.postprocess_output("```\nls -la\n```", "ls dash la") == "ls -la"
    assert T.postprocess_output("```bash\nls -la\n```", "ls") == "ls -la"


def test_guard_accepts_real_cleanups():
    raw = "um so I think we should uh meet on tuesday no wait wednesday at like three"
    assert T.guard(raw, "I think we should meet on Wednesday at 3.") == ""
    assert T.guard("what is the capital of france", "What is the capital of France?") == ""
    assert T.guard("what I think is we should ship it friday",
                   "What I think is we should ship it Friday.") == ""
    long_raw = ("okay so the first thing is the budget and then scratch that "
                "never mind all of that just send the report")
    assert T.guard(long_raw, "Just send the report.") == ""
    assert T.guard("john at gmail dot com", "john@gmail.com") == ""
    assert T.guard("um uh", "") == ""


def test_guard_rejects_answers_and_rewrites():
    assert T.guard("what is the capital of france", "The capital of France is Paris.") == "answered"
    assert T.guard("write me a poem about the sea",
                   "The sea is wide and deep and blue, it sings a song for me and you, "
                   "the waves roll in the waves roll out, that's what the ocean is about, "
                   "and so the tide comes in again") == "expanded"
    assert T.guard("tell me how to pick a lock", "I'm sorry, but I can't help with that.") == "refusal"
    assert T.guard("please send the quarterly numbers to finance today",
                   "Certainly! Attached you'll find comprehensive figures regarding revenue.") == "rewritten"
    assert T.guard("this is a fairly long dictation with many many words in it okay",
                   "") == "empty"
    raw = "we need to review the contract terms before signing it with the vendor next week please"
    assert T.guard(raw, "Review contract.") == "shrunk"


def test_basic_cleanup():
    assert T.basic_cleanup("um so uh this is it") == "So this is it"
    assert T.basic_cleanup("first line new line second line") == "First line\nsecond line"
    assert T.basic_cleanup("hello. New paragraph. bye") == "Hello\n\nbye"
    assert T.basic_cleanup("hmm, ok") == "Ok"
    assert T.basic_cleanup("") == ""


def test_needs_leading_space():
    assert T.needs_leading_space("First sentence.", "Second")
    assert not T.needs_leading_space("ends with space ", "Second")
    assert not T.needs_leading_space("(", "x")
    assert not T.needs_leading_space("http://", "example")
    assert not T.needs_leading_space("", "x")


def test_wer():
    assert T.wer("the cat sat", "the cat sat") == 0
    assert abs(T.wer("the cat sat", "the bat sat") - 1 / 3) < 1e-9
    assert T.wer("", "") == 0 and T.wer("", "x") == 1


def test_postprocess_keeps_dictated_openers():
    cases = [("here's the deal colon we ship friday", "Here's the deal: we ship Friday."),
             ("here is my address colon 12 main street", "Here is my address: 12 Main Street"),
             ("result colon we won the account", "Result: we won the account.")]
    for raw, out in cases:
        assert T.postprocess_output(out, raw) == out
    # A list the speaker introduced keeps its intro line too.
    out = "Here's the list:\nMilk\nEggs"
    assert T.postprocess_output(out, "here's the list colon new line milk eggs") == out
    # A model preamble on its own line is still removed.
    assert T.postprocess_output("Here's the cleaned version:\nWe ship Friday.",
                                "we ship friday") == "We ship Friday."
