"""deepdive.py tests with a fake client; no API calls and no web searches are made."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JOBSCOUT_RUBRIC"] = str(ROOT / "rubric.example.md")  # before importing score and deepdive
os.environ["JOBSCOUT_FACTS"] = str(ROOT / "facts.example.md")

import db  # noqa: E402
import deepdive as dd  # noqa: E402
from parse import Listing  # noqa: E402

# A fixed copy of facts.md's structure, so draft-check tests don't change when facts.md is edited.
FACTS = r"""\# Verified facts

\- Business Analyst at Northwind Platforms. 6.5 years at Northwind Platforms in total

\- Analyst on a 9-person delivery team without formal line management

\- Contoso Singapore reporting rebuild: about a 64% reduction in reconciliation time

\- Lead analyst on a US$1.2M lending system pilot for Fabrikam Trust Bank (a regional bank): scoped and designed 140 story points across three releases

\## Never claim

\- Direct reports or formal line management
"""
EXTRA = ["Harbor Partners"]  # as if from blocked_names.txt
URL_A = "https://example.com/about"
URL_B = "https://news.example.com/funding"


def usage(searches=0):
    return NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches))


def search_use(id_, query):
    return NS(type="server_tool_use", name="web_search", id=id_, input={"query": query})


def search_result(id_, urls):
    return NS(type="web_search_tool_result", tool_use_id=id_, content=[NS(url=u, title="t") for u in urls])


def search_error(id_, code="max_uses_exceeded"):
    return NS(type="web_search_tool_result", tool_use_id=id_, content=NS(type="error", error_code=code))


def text(t):
    return NS(type="text", text=t)


def message(content, stop="end_turn", searches=0):
    return NS(content=content, stop_reason=stop, usage=usage(searches), stop_details=None)


def parsed(obj, stop="end_turn"):
    return NS(content=[], stop_reason=stop, usage=usage(), stop_details=None, parsed_output=obj)


BRIEF = dd.CompanyBrief(
    company="Acme Wealth", what_they_do="Wealth platform for advisers", company_type="software product company",
    claims=[dd.SourcedClaim(claim="Acme runs a wealth platform for 300 advisers", source_url=URL_A),
            dd.SourcedClaim(claim="Acme raised a Series B", source_url=URL_B)],
    unknowns=["headcount"],
    searches=[dd.SearchReason(query="Acme Wealth NZ", reason="what the company does"),
              dd.SearchReason(query="Acme Wealth funding", reason="financial position")])

GOOD_COVER = ("Cut reconciliation time by about 64% by rebuilding a reporting process. "
              "That is the kind of platform problem Acme's adviser platform will keep throwing up. "
              "I've spent 6.5 years at Northwind Platforms as a business analyst.\n\nSam")
GOOD_OUTREACH = ("Scoped and designed 140 story points across three releases for a lending system pilot. "
                 "Keen to talk about the product role at Acme.\n\nSam")


def drafts(cover=GOOD_COVER, outreach=GOOD_OUTREACH, facts_used=None, claims=None):
    facts_used = facts_used or ["about a 64% reduction in reconciliation time"]
    claims = claims if claims is not None else [dd.SourcedClaim(claim="Acme runs a wealth platform", source_url=URL_A)]
    return dd.Drafts(
        cover_letter=dd.Draft(text=cover, facts_used=facts_used, employer_claims=claims),
        outreach=dd.Draft(text=outreach, facts_used=["scoped and designed 140 story points across three releases"],
                          employer_claims=[]))


class FakeClient:
    """create() returns research messages in turn; parse() returns objects in turn."""

    def __init__(self, creates=(), parses=()):
        self.creates, self.parses = list(creates), list(parses)
        self.create_calls, self.parse_calls = [], []
        self.beta = NS(messages=NS(create=self._create, parse=self._parse))

    def _create(self, **kw):
        self.create_calls.append(kw)
        return self.creates.pop(0)

    def _parse(self, **kw):
        self.parse_calls.append(kw)
        out = self.parses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


JOB = {"job_id": "42", "title": "Senior Product Owner", "company": "Acme Wealth", "location": "Auckland"}


def setUpModule():
    global _facts_dir, _patches
    _facts_dir = tempfile.TemporaryDirectory()
    path = Path(_facts_dir.name) / "facts.md"
    path.write_text(FACTS, encoding="utf-8")
    names = Path(_facts_dir.name) / "blocked_names.txt"
    names.write_text("\n".join(EXTRA) + "\n", encoding="utf-8")
    _patches = [mock.patch.object(dd, "FACTS_FILE", path), mock.patch.object(dd, "NAMES_FILE", names),
                mock.patch.dict(dd.os.environ, {dd.SIGN_NAME_VAR: "Sam"})]
    for patch in _patches:
        patch.start()


def tearDownModule():
    for patch in _patches:
        patch.stop()
    _facts_dir.cleanup()


class ClientNameTests(unittest.TestCase):
    def test_names_from_facts_plus_extra(self):
        names = dd.client_names(FACTS, EXTRA)
        self.assertEqual(names, ["Contoso", "Fabrikam Trust Bank", "Harbor Partners"])

    def test_allowed_names_are_not_blocked(self):
        self.assertEqual(dd.client_names(FACTS, EXTRA, ["Contoso"]), ["Fabrikam Trust Bank", "Harbor Partners"])

    def test_load_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "blocked_names.txt"
            path.write_text("# comment\n\nHarbor Partners\nallow: Northwind Platforms\n", encoding="utf-8")
            self.assertEqual(dd.load_names(path), (["Harbor Partners"], ["Northwind Platforms"]))
            self.assertEqual(dd.load_names(Path(d) / "missing.txt"), ([], []))

    def test_example_facts_file(self):
        path = ROOT / "facts.example.md"
        with mock.patch.dict(os.environ, {"JOBSCOUT_FACTS": str(path)}):
            self.assertEqual(dd.score.path_from_env("JOBSCOUT_FACTS", "facts.md"), path)
        facts = path.read_text(encoding="utf-8")
        self.assertEqual(dd.client_names(facts), ["Pohutukawa Mutual Retail", "Tasman"])


class ResearchTests(unittest.TestCase):
    def test_searches_logged_with_results_and_errors(self):
        client = FakeClient(creates=[message([
            text("Looking at the company first."), search_use("s1", "Acme Wealth NZ"),
            search_result("s1", [URL_A]), search_use("s2", "Acme Wealth funding"),
            search_result("s2", [URL_B]), search_use("s3", "Acme extra"), search_error("s3"),
            text("## Search log\n...")], searches=3)])
        u = dd.Usage()
        res = dd.research(client, u, JOB, "JD text")
        self.assertEqual([(s.seq, s.query) for s in res.searches],
                         [(1, "Acme Wealth NZ"), (2, "Acme Wealth funding"), (3, "Acme extra")])
        self.assertEqual(res.searches[0].urls, [URL_A])
        self.assertEqual(res.searches[2].error, "max_uses_exceeded")
        self.assertEqual(res.result_urls, {URL_A, URL_B})
        self.assertEqual(u.web_searches, 3)

    def test_request_blocks_job_sites_caps_searches_and_uses_fallback(self):
        client = FakeClient(creates=[message([text("done")])])
        dd.research(client, dd.Usage(), JOB, "JD")
        call = client.create_calls[0]
        tool = call["tools"][0]
        self.assertEqual(tool["type"], "web_search_20250305")
        self.assertEqual(tool["max_uses"], 6)
        self.assertIn("linkedin.com", tool["blocked_domains"])
        self.assertIn("seek.co.nz", tool["blocked_domains"])
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["fallbacks"], "default")
        self.assertEqual(call["betas"], ["server-side-fallback-2026-07-01"])

    def test_pause_turn_resumes_without_extra_user_message(self):
        first = message([search_use("s1", "q1"), search_result("s1", [URL_A])], stop="pause_turn")
        second = message([text("notes")])
        client = FakeClient(creates=[first, second])
        res = dd.research(client, dd.Usage(), JOB, "JD")
        self.assertEqual(len(client.create_calls), 2)
        resumed = client.create_calls[1]["messages"]
        self.assertEqual([m["role"] for m in resumed], ["user", "assistant"])
        self.assertEqual(res.text, "notes")

    def test_refusal_raises(self):
        client = FakeClient(creates=[message([], stop="refusal")])
        with self.assertRaises(dd.DeepDiveError):
            dd.research(client, dd.Usage(), JOB, "JD")


class BriefTests(unittest.TestCase):
    def research(self):
        return dd.Research(text="notes", searches=[dd.Search(1, "Acme Wealth NZ", [URL_A]),
                                                   dd.Search(2, "Acme Wealth funding", [URL_B])])

    def test_reasons_attached_to_searches_that_ran(self):
        res = self.research()
        brief, warnings = dd.make_brief(FakeClient(parses=[parsed(BRIEF)]), dd.Usage(), JOB, res)
        self.assertEqual(warnings, [])
        self.assertEqual([s.reason for s in res.searches], ["what the company does", "financial position"])

    def test_missing_reason_and_phantom_search_are_flagged(self):
        res = self.research()
        warnings = dd.attach_reasons(res.searches, [dd.SearchReason(query="acme wealth nz", reason="r"),
                                                    dd.SearchReason(query="never ran", reason="r")])
        self.assertEqual(len(warnings), 2)
        self.assertIsNone(res.searches[1].reason)

    def test_unsourced_claim_retried_then_dropped(self):
        bad = BRIEF.model_copy(deep=True)
        bad.claims.append(dd.SourcedClaim(claim="Acme has 900 staff", source_url="https://made.up/x"))
        client = FakeClient(parses=[parsed(bad), parsed(bad.model_copy(deep=True))])
        brief, warnings = dd.make_brief(client, dd.Usage(), JOB, self.research())
        self.assertEqual(len(client.parse_calls), 2)
        self.assertNotIn("https://made.up/x", {c.source_url for c in brief.claims})
        self.assertTrue(any("dropped claim" in w for w in warnings))


class CheckTests(unittest.TestCase):
    names = dd.client_names(FACTS, EXTRA)

    def problems(self, d):
        return dd.check_drafts(d, FACTS, "We want 5+ years experience.", BRIEF, self.names)

    def test_clean_drafts_pass(self):
        self.assertEqual(self.problems(drafts()), [])

    def test_invented_number(self):
        p = self.problems(drafts(cover=GOOD_COVER.replace("6.5 years", "37 years")))
        self.assertTrue(any("number 37" in x for x in p))

    def test_employer_numbers_from_brief_allowed(self):
        self.assertEqual(self.problems(drafts(outreach="Acme serves 300 advisers; I scoped 140 story points.\n\nSam")), [])

    def test_client_names_blocked_including_first_word(self):
        for name in ("Contoso", "Fabrikam", "Harbor Partners"):
            with self.subTest(name=name):
                p = self.problems(drafts(outreach=f"I delivered for {name}.\n\nSam"))
                self.assertTrue(any("names a client" in x for x in p))

    def test_employer_not_blocked(self):
        self.assertEqual(self.problems(drafts(outreach="Business Analyst at Northwind Platforms, scoped 140 story points.\n\nSam")), [])

    def test_facts_used_must_be_quotes(self):
        p = self.problems(drafts(facts_used=["Managed a team of 9 direct reports"]))
        self.assertTrue(any("not a quote" in x for x in p))

    def test_employer_claim_must_be_in_brief(self):
        p = self.problems(drafts(claims=[dd.SourcedClaim(claim="Acme is hiring fast", source_url="https://other")]))
        self.assertTrue(any("not in the company brief" in x for x in p))

    def test_tone_filler_opener_sign_off_gap_opener(self):
        cases = {"filler": "I am writing to apply. " + GOOD_COVER,
                 "sign-off": GOOD_COVER.replace("Sam", "Kind regards,\nSam"),
                 "gap": "Although I have not worked at a bank, I cut reconciliation time by 64%.\n\nSam"}
        for label, cover in cases.items():
            with self.subTest(label=label):
                self.assertTrue(self.problems(drafts(cover=cover)))


class WriteDraftsTests(unittest.TestCase):
    names = dd.client_names(FACTS, EXTRA)

    def test_verifier_issue_triggers_one_retry_then_success(self):
        issue = dd.Verdict(issues=[dd.Issue(draft="outreach", sentence="x", problem="unsupported claim")])
        client = FakeClient(parses=[parsed(drafts()), parsed(issue), parsed(drafts()), parsed(dd.Verdict(issues=[]))])
        out, problems = dd.write_drafts(client, dd.Usage(), JOB, "JD", BRIEF, self.names,
                                          sign_name="Sam", allowed_names=[])
        self.assertEqual(problems, [])
        self.assertIn("unsupported claim", client.parse_calls[2]["messages"][0]["content"])

    def test_fails_after_retry(self):
        bad = drafts(cover=GOOD_COVER.replace("6.5", "20"))
        client = FakeClient(parses=[parsed(bad), parsed(bad)])
        out, problems = dd.write_drafts(client, dd.Usage(), JOB, "JD", BRIEF, self.names,
                                          sign_name="Sam", allowed_names=[])
        self.assertTrue(problems)
        self.assertEqual(len(client.parse_calls), 2)  # verifier never runs on drafts that fail code checks


class ReviewAndRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.save_email(self.conn, {"id": "m", "internal_date": 1, "from": "x", "subject": "s"}, "parsed",
                      [Listing("42", "Senior Product Owner", "Acme Wealth", "Auckland", "u")], [])
        self.patch = mock.patch.object(dd, "DRAFTS_DIR", Path(self.tmp.name) / "drafts")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_approve_edit_skip_logged_and_saved(self):
        answers = iter(["e", "s"])
        def edit(path):
            path.write_text("Edited cover.\n\nSam", encoding="utf-8")
        with mock.patch("builtins.print"):
            decisions = dd.review(self.conn, "42", None, drafts(), "sha", ask=lambda _: next(answers), edit=edit)
        self.assertEqual(decisions, {"cover_letter": "edited", "outreach": "skipped"})
        rows = {r["kind"]: r for r in self.conn.execute("SELECT * FROM drafts")}
        self.assertEqual(rows["cover_letter"]["final_text"], "Edited cover.\n\nSam")
        self.assertIsNone(rows["outreach"]["final_text"])
        saved = (dd.DRAFTS_DIR / "42-cover-letter.md").read_text(encoding="utf-8")
        self.assertIn("Edited cover.", saved)
        self.assertIn(URL_A, saved)
        self.assertFalse((dd.DRAFTS_DIR / "42-outreach.md").exists())

    def test_run_confirms_before_any_api_call(self):
        jd = Path(self.tmp.name) / "jd.txt"
        jd.write_text("Senior Product Owner, wealth platform. 5+ years.", encoding="utf-8")
        events = []
        client = FakeClient(
            creates=[message([search_use("s1", "Acme Wealth NZ"), search_result("s1", [URL_A]),
                              search_use("s2", "Acme Wealth funding"), search_result("s2", [URL_B]),
                              text("notes")], searches=2)],
            parses=[parsed(BRIEF), parsed(drafts()), parsed(dd.Verdict(issues=[]))])
        def ask(prompt):
            events.append(("ask", prompt))
            return "" if prompt.startswith("Press Enter") else "a"
        def make_client():
            events.append(("client", None))
            return client
        with mock.patch("builtins.print"):
            decisions = dd.run(self.conn, "42", jd, ask=ask, make_client=make_client)
        self.assertEqual(events[0][0], "ask")
        self.assertTrue(events[0][1].startswith("Press Enter"))
        self.assertEqual(events[1][0], "client")
        self.assertEqual(decisions, {"cover_letter": "approved", "outreach": "approved"})
        run_row = self.conn.execute("SELECT * FROM research_runs").fetchone()
        self.assertEqual((run_row["status"], run_row["web_searches"]), ("ok", 2))
        searches = self.conn.execute("SELECT query, reason FROM searches ORDER BY seq").fetchall()
        self.assertEqual([tuple(s) for s in searches],
                         [("Acme Wealth NZ", "what the company does"), ("Acme Wealth funding", "financial position")])
        self.assertIsNotNone(self.conn.execute("SELECT jd_text FROM job_descriptions WHERE job_id='42'").fetchone())

    def test_cancel_at_confirmation_makes_no_api_call(self):
        jd = Path(self.tmp.name) / "jd.txt"
        jd.write_text("JD", encoding="utf-8")
        def ask(_):
            raise KeyboardInterrupt
        def make_client():
            raise AssertionError("client created after cancel")
        with mock.patch("builtins.print"), self.assertRaises(KeyboardInterrupt):
            dd.run(self.conn, "42", jd, ask=ask, make_client=make_client)
        self.assertIsNone(self.conn.execute("SELECT * FROM research_runs").fetchone())

    def test_missing_sign_name_stops_before_any_call(self):
        jd = Path(self.tmp.name) / "jd.txt"
        jd.write_text("JD", encoding="utf-8")
        def fail(*_):
            raise AssertionError("got past the sign-name check")
        with mock.patch.dict(dd.os.environ, {dd.SIGN_NAME_VAR: ""}), mock.patch.object(dd, "load_dotenv"), \
                self.assertRaises(dd.DeepDiveError):
            dd.run(self.conn, "42", jd, ask=fail, make_client=fail)
        self.assertIsNone(self.conn.execute("SELECT * FROM research_runs").fetchone())

    def test_unknown_job_and_empty_jd(self):
        jd = Path(self.tmp.name) / "jd.txt"
        jd.write_text("", encoding="utf-8")
        with self.assertRaises(dd.DeepDiveError):
            dd.run(self.conn, "999", jd, ask=lambda _: "")
        with self.assertRaises(dd.DeepDiveError):
            dd.run(self.conn, "42", jd, ask=lambda _: "")


class CostTests(unittest.TestCase):
    def test_estimate_range_and_usage_cost(self):
        low, high = dd.estimate_cost("x" * 4000, FACTS)
        self.assertLess(0, low)
        self.assertLess(low, high)
        u = dd.Usage()
        u.add(usage(searches=2))
        self.assertAlmostEqual(u.cost, 1000 * 4 / 1e6 + 200 * 20 / 1e6 + 2 * 0.01)


if __name__ == "__main__":
    unittest.main()
