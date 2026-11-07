"""Tests for fetch_library audio reuse logic."""
import os
import pytest

from fetch_library import (
    parse_paper_stem,
    find_audio_donor,
    fill_missing_audio,
    DATA,
)


class TestParsePaperStem:
    def test_standard_paper(self):
        assert parse_paper_stem('2024-12-CET4-1') == ('2024', '12', 'CET4', '1')
        assert parse_paper_stem('2019-06-CET6-3') == ('2019', '06', 'CET6', '3')

    def test_merged_paper(self):
        assert parse_paper_stem('2022-09-CET4-2-3') == ('2022', '09', 'CET4', '2-3')
        assert parse_paper_stem('2023-03-CET6-2-3') == ('2023', '03', 'CET6', '2-3')

    def test_invalid(self):
        assert parse_paper_stem('invalid') is None
        assert parse_paper_stem('2024-12-CET5-1') is None
        assert parse_paper_stem('') is None


class TestFindAudioDonor:
    def test_set3_prefers_set2(self):
        available = {'2024-12-CET4-1', '2024-12-CET4-2'}
        assert find_audio_donor('2024-12-CET4-3', available) == '2024-12-CET4-2'

    def test_set3_falls_back_to_set1(self):
        available = {'2024-12-CET4-1'}
        assert find_audio_donor('2024-12-CET4-3', available) == '2024-12-CET4-1'

    def test_set3_no_sibling(self):
        available = set()
        assert find_audio_donor('2024-12-CET4-3', available) is None

    def test_merged_uses_set1(self):
        available = {'2022-09-CET4-1', '2022-09-CET4-2'}
        assert find_audio_donor('2022-09-CET4-2-3', available) == '2022-09-CET4-1'

    def test_merged_no_set1(self):
        available = {'2022-09-CET4-2'}
        assert find_audio_donor('2022-09-CET4-2-3', available) is None

    def test_set2_falls_back_to_set1(self):
        available = {'2024-12-CET4-1'}
        assert find_audio_donor('2024-12-CET4-2', available) == '2024-12-CET4-1'

    def test_set1_has_no_donor(self):
        available = {'2024-12-CET4-2', '2024-12-CET4-3'}
        assert find_audio_donor('2024-12-CET4-1', available) is None

    def test_different_session_not_matched(self):
        available = {'2024-06-CET4-2'}
        assert find_audio_donor('2024-12-CET4-3', available) is None

    def test_different_level_not_matched(self):
        available = {'2024-12-CET6-2'}
        assert find_audio_donor('2024-12-CET4-3', available) is None


class TestFillMissingAudio:
    @pytest.fixture
    def audio_setup(self, tmp_path, monkeypatch):
        """Set up a temp data directory with PDFs and some audio files."""
        data_dir = tmp_path / 'data'
        data_dir.mkdir()
        audio_dir = data_dir / 'audio'
        audio_dir.mkdir()

        monkeypatch.setattr('fetch_library.DATA', str(data_dir))

        pdfs = [
            '2024-12-CET4-1.pdf',
            '2024-12-CET4-2.pdf',
            '2024-12-CET4-3.pdf',
            '2022-09-CET6-1.pdf',
            '2022-09-CET6-2-3.pdf',
        ]
        for pdf in pdfs:
            (data_dir / pdf).write_bytes(b'%PDF')

        (audio_dir / '2024-12-CET4-1.mp3').write_bytes(b'ID3audio1')
        (audio_dir / '2024-12-CET4-2.mp3').write_bytes(b'ID3audio2')
        (audio_dir / '2022-09-CET6-1.mp3').write_bytes(b'ID3audio6')

        return data_dir, audio_dir

    def test_fills_set3_from_set2(self, audio_setup):
        data_dir, audio_dir = audio_setup
        reused = fill_missing_audio()

        assert ('2024-12-CET4-3', '2024-12-CET4-2') in reused
        assert (audio_dir / '2024-12-CET4-3.mp3').exists()
        assert (audio_dir / '2024-12-CET4-3.mp3').read_bytes() == b'ID3audio2'

    def test_fills_merged_from_set1(self, audio_setup):
        data_dir, audio_dir = audio_setup
        reused = fill_missing_audio()

        assert ('2022-09-CET6-2-3', '2022-09-CET6-1') in reused
        assert (audio_dir / '2022-09-CET6-2-3.mp3').exists()
        assert (audio_dir / '2022-09-CET6-2-3.mp3').read_bytes() == b'ID3audio6'

    def test_does_not_overwrite_existing(self, audio_setup):
        data_dir, audio_dir = audio_setup
        (audio_dir / '2024-12-CET4-3.mp3').write_bytes(b'existing')
        reused = fill_missing_audio()

        assert not any(dest == '2024-12-CET4-3' for dest, _ in reused)
        assert (audio_dir / '2024-12-CET4-3.mp3').read_bytes() == b'existing'

    def test_dry_run_does_not_copy(self, audio_setup):
        data_dir, audio_dir = audio_setup
        reused = fill_missing_audio(dry_run=True)

        assert len(reused) == 2
        assert not (audio_dir / '2024-12-CET4-3.mp3').exists()
        assert not (audio_dir / '2022-09-CET6-2-3.mp3').exists()

    def test_no_pdfs_no_action(self, tmp_path, monkeypatch):
        data_dir = tmp_path / 'data'
        data_dir.mkdir()
        monkeypatch.setattr('fetch_library.DATA', str(data_dir))

        reused = fill_missing_audio()
        assert reused == []

    def test_all_audio_present_no_action(self, audio_setup):
        data_dir, audio_dir = audio_setup
        (audio_dir / '2024-12-CET4-3.mp3').write_bytes(b'ID3')
        (audio_dir / '2022-09-CET6-2-3.mp3').write_bytes(b'ID3')

        reused = fill_missing_audio()
        assert reused == []
