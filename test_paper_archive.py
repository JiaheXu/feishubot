from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paper_sources


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "papers"
        self.pdf = b"%PDF-1.4\nreal PDF bytes for storage test\n%%EOF"

    def test_arxiv_html_also_archives_pdf_with_year_and_title(self):
        html = b'<html><title>Scaling Online Exploration for Robot Agents</title><article>Results.</article></html>'
        with patch("paper_sources.download", return_value=(html, "text/html", "https://arxiv.org/html/2610.22496v1")):
            source = paper_sources.fetch_source("https://arxiv.org/abs/2610.22496v1")
        with patch("paper_sources.download", return_value=(self.pdf, "application/pdf", "https://arxiv.org/pdf/2610.22496v1")):
            saved, errors = paper_sources.archive_papers(source, self.root)
        expected = self.root / "2026-scaling-online-exploration-for-robot-agents.pdf"
        self.assertEqual(saved, [str(expected)])
        self.assertEqual(errors, [])
        self.assertEqual(expected.read_bytes(), self.pdf)
        self.assertEqual(expected.stat().st_mode & 0o777, 0o600)

    def test_same_title_does_not_overwrite_a_different_pdf(self):
        source = {"url": "https://example.org/paper.pdf", "title": "../MimicPlay: Long Horizon", "year": "2023"}
        with patch("paper_sources.download", return_value=(self.pdf, "application/pdf", source["url"])):
            first, _ = paper_sources.archive_papers(source, self.root)
            again, _ = paper_sources.archive_papers(source, self.root)
        other = self.pdf + b"different version"
        with patch("paper_sources.download", return_value=(other, "application/pdf", source["url"])):
            second, _ = paper_sources.archive_papers(source, self.root)
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first[0]).parent, self.root)
        self.assertEqual(Path(first[0]).read_bytes(), self.pdf)
        self.assertEqual(Path(second[0]).read_bytes(), other)

    def test_unknown_or_invalid_year_uses_current_year(self):
        for year in (None, "", "unknown-year", "invalid"):
            source = {"url": "https://example.org/paper.pdf", "title": "A paper", "year": year}
            with self.subTest(year=year), \
                    patch("paper_sources.datetime") as clock, \
                    patch("paper_sources.download", return_value=(self.pdf, "application/pdf", source["url"])):
                clock.now.return_value = datetime(2027, 1, 1)
                saved, errors = paper_sources.archive_papers(source, self.root)
                self.assertEqual(errors, [])
                self.assertEqual(Path(saved[0]).name, "2027-a-paper.pdf")
                self.assertEqual(Path(saved[0]).read_bytes(), self.pdf)

    def test_html_error_response_is_not_saved_as_a_pdf(self):
        source = {"url": "https://example.org/paper.pdf", "title": "A paper", "year": "2023"}
        with patch("paper_sources.download", return_value=(b"<html>Login required</html>", "application/pdf", source["url"])):
            saved, errors = paper_sources.archive_papers(source, self.root)
        self.assertEqual(saved, [])
        self.assertTrue(errors)
        self.assertFalse(self.root.exists())

    def test_blog_main_paper_link_is_archived_but_not_its_bibliography(self):
        html = b'''<html><title>A project</title><article>
        <a href="https://arxiv.org/abs/2610.22496v1">Paper</a>
        <a href="https://example.org/related.pdf">[1] Related work</a></article></html>'''
        with patch("paper_sources.download", return_value=(html, "text/html", "https://example.org/project")):
            source = paper_sources.fetch_source("https://example.org/project")
        self.assertEqual(source["paper_urls"], ["https://arxiv.org/abs/2610.22496v1"])
        paper = {"url": source["paper_urls"][0], "title": "Robot Agents", "year": "2026"}
        with patch("paper_sources.fetch_source", return_value=paper), \
                patch("paper_sources.download", return_value=(self.pdf, "application/pdf", "https://arxiv.org/pdf/2610.22496v1")):
            saved, errors = paper_sources.archive_papers(source, self.root)
        self.assertEqual(Path(saved[0]).name, "2026-robot-agents.pdf")
        self.assertEqual(errors, [])

    def test_project_paper_buttons_support_icons_arxiv_and_accessible_labels(self):
        html = '''<html><title>Project</title><article>
        <a href="../paper.pdf?v=1"><span>📄 Read the Paper</span></a>
        <a href="report.pdf" aria-label="Technical report"><svg></svg></a>
        <a href="https://arxiv.org/abs/2610.22496"><span>arXiv</span></a>
        <a href="/reference.pdf">[1] Related work</a>
        </article></html>'''.encode()
        with patch("paper_sources.download", return_value=(html, "text/html", "https://example.org/project/")):
            source = paper_sources.fetch_source("https://example.org/project/")
        self.assertEqual(source["paper_urls"], [
            "https://example.org/paper.pdf?v=1", "https://example.org/project/report.pdf",
            "https://arxiv.org/abs/2610.22496",
        ])

    def test_arxiv_bibliography_is_not_treated_as_project_paper_links(self):
        html = b'''<html><title>Robot Learning</title><article>Main paper text.</article>
        <section id="references"><a href="https://arxiv.org/abs/2401.00001">arXiv</a></section></html>'''
        with patch("paper_sources.download", return_value=(html, "text/html", "https://arxiv.org/html/2610.22496")):
            source = paper_sources.fetch_source("https://arxiv.org/abs/2610.22496")
        self.assertEqual(source["pdf_url"], "https://arxiv.org/pdf/2610.22496")
        self.assertEqual(source["paper_urls"], [])

    def test_article_without_associated_paper_does_not_create_fake_pdf(self):
        with patch("paper_sources.download") as download:
            self.assertEqual(paper_sources.archive_papers({"url": "https://example.org/blog"}, self.root), ([], []))
        download.assert_not_called()

    def test_publication_metadata_produces_requested_mimicplay_filename(self):
        html = b'''<html><title>Project page</title>
        <meta name="citation_title" content="MimicPlay: Long-Horizon Imitation Learning by Watching Human Play">
        <meta name="citation_publication_date" content="2023/02/01">
        <meta name="citation_pdf_url" content="/paper.pdf"><article>Paper results.</article></html>'''
        with patch("paper_sources.download", return_value=(html, "text/html", "https://example.org/project")):
            source = paper_sources.fetch_source("https://example.org/project")
        with patch("paper_sources.download", return_value=(self.pdf, "application/pdf", "https://example.org/paper.pdf")):
            saved, errors = paper_sources.archive_papers(source, self.root)
        self.assertEqual(Path(saved[0]).name,
                         "2023-mimicplay-long-horizon-imitation-learning-by-watching-human-play.pdf")
        self.assertEqual(errors, [])
