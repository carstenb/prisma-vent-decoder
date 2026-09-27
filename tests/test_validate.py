"""Tests for the cross-channel validation pass.

Fixtures are generated at runtime and shaped so the physics is known: a square
breathing waveform at a chosen rate, with a chosen inspiratory flow.
"""

import io
import re
from datetime import date, datetime

import pytest

from prisma_vent.validate import (
    DEVICE_RATE_ACCURACY_PER_MIN,
    EXIT_OK,
    EXIT_STRUCTURAL,
    MIN_SECONDS_FOR_RATE,
    CheckStatus,
    _check,
    main,
)
from synthetic import SyntheticSignal, build_wmedf, build_day_archive

ARCHIVE_DATE = date(2020, 3, 4)


def _breathing_session(
    n_records: int,
    *,
    record_duration: str = "1",
    breaths_per_minute: int = 15,
    reported_rate: float = 15.0,
    inspiratory_flow: float = 30.0,
    reported_tidal: float = 200.0,
    leak: float = 5.0,
    targeting: bool | None = None,
    tidal_zero_samples: int = 0,
    start: datetime = datetime(2020, 3, 4, 22, 0, 0),
) -> bytes:
    """A session whose channels agree, or disagree, by construction.

    ``record_duration`` is written into the header, and the sample counts are
    scaled to keep the *real* rates fixed: a two-second record carries twice
    the samples for the same 10 Hz. That is what makes a decoder assuming
    one-second records come out wrong by exactly that factor.
    """
    seconds_per_record = float(record_duration)
    phase_spr = int(round(10 * seconds_per_record))    # 10 Hz
    slow_spr = int(round(1 * seconds_per_record))      # 1 Hz
    period = int(round(10 * 60 / breaths_per_minute))  # samples per breath at 10 Hz
    half = period // 2

    phase, flow = [], []
    for sample in range(n_records * phase_spr):
        inspiring = (sample % period) < half
        phase.append(2 if inspiring else 1)
        flow.append(int(inspiratory_flow * 10) if inspiring else -int(inspiratory_flow * 10))

    # Zeroed from the front, so a caller knows exactly which samples they are.
    tidal_data = [int(reported_tidal * 10)] * (n_records * slow_spr)
    for sample in range(min(tidal_zero_samples, len(tidal_data))):
        tidal_data[sample] = 0

    signals = [
        SyntheticSignal("Breath Phase", "", 0.0, 1.0, 1, 2, phase_spr, 1, data=phase),
        SyntheticSignal(
            "Patient Flow", "l/min", -120.0, 250.0, -1200, 2500, phase_spr, 2, data=flow
        ),
        SyntheticSignal(
            "Frequency", "1/min", 0.0, 80.0, 0, 800, slow_spr, 2,
            data=[int(reported_rate * 10)] * (n_records * slow_spr),
        ),
        SyntheticSignal(
            "Tidal Volume", "ml", 0.0, 3000.0, 0, 30000, slow_spr, 2,
            data=tidal_data,
        ),
        SyntheticSignal(
            "TotalLeakage", "l/min", 0.0, 300.0, 0, 3000, slow_spr, 2,
            data=[int(leak * 10)] * (n_records * slow_spr),
        ),
    ]
    if targeting is not None:
        # A state channel, zero or one, despite the name. Written last so the
        # other channels keep their indices in every existing test.
        signals.append(
            SyntheticSignal(
                "Target Volume", "", 0.0, 1.0, 0, 1, slow_spr, 1,
                data=[1 if targeting else 0] * (n_records * slow_spr),
            )
        )
    return build_wmedf(
        signals, n_records, start=start, record_duration=record_duration,
    )


def _archive(tmp_path, **kwargs):
    import zipfile
    from synthetic import session_event_xml

    tmp_path.mkdir(parents=True, exist_ok=True)
    # Long enough for the rate comparison's own precondition.
    n_records = kwargs.pop("n_records", 400)
    seconds = n_records * float(kwargs.get("record_duration", "1"))
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", _breathing_session(n_records, **kwargs))
        zf.writestr("event_0001.xml", session_event_xml(start, int(seconds)))
    return path


def run(*args):
    out = io.StringIO()
    return main([str(a) for a in args], out=out), out.getvalue()


def test_agreeing_channels_pass(tmp_path):
    """Rate and volume both derived from the same waveform must agree."""
    status, text = run(_archive(tmp_path))
    assert status == EXIT_OK
    assert "No cross-channel disagreement" in text


def test_breath_rate_disagreement_is_reported_not_failed(tmp_path):
    """The waveform breathes at 15/min while the rate channel claims 45.

    That is the shape of a plausible misparse, and this check is the only one
    that can see it — but it cannot tell it apart from on-demand ventilation,
    and no published figure bounds within-session rate variation. So the
    difference is printed and the run still succeeds.
    """
    status, text = run(_archive(tmp_path, breaths_per_minute=15, reported_rate=45.0))
    assert status == EXIT_OK
    assert "breath rate reported (not asserted)" in text
    assert "counted 15" in text and "reported 45.00/min" in text


def test_the_breath_rate_status_is_reported_not_passed(tmp_path):
    """`reported` and `passed` are different claims and must stay different.

    Calling this passed would credit the decode with surviving a test that was
    never applied to it.
    """
    session = _check(_archive(tmp_path)).sessions[0]
    assert session.breath.status is CheckStatus.REPORTED
    assert session.breath.status is not CheckStatus.PASSED


def test_a_small_rate_difference_is_tolerated(tmp_path):
    """Inside the device's own stated accuracy, so not a decode failure."""
    inside = 15.0 + DEVICE_RATE_ACCURACY_PER_MIN / 2
    status, _ = run(_archive(tmp_path, breaths_per_minute=15, reported_rate=inside))
    assert status == EXIT_OK


# --------------------------------------------------------------------------
# The tolerance is absolute, and derived rather than fitted
# --------------------------------------------------------------------------


def test_the_tolerance_is_absolute_not_a_percentage():
    """A percentage means different things at different rates.

    The manufacturer states ±0.5 breaths per minute, which is 10 % at 5 bpm
    and 2.5 % at 20. A fixed 5 % gate — which this module used to apply — is
    therefore too loose at the bottom of the device's range and too tight at
    the top.
    """
    from prisma_vent.validate import rate_tolerance_per_min

    # Same span, so the same absolute tolerance whatever the rate.
    assert rate_tolerance_per_min(10.0) == rate_tolerance_per_min(10.0)
    assert rate_tolerance_per_min(10.0) == pytest.approx(0.5 + 0.1)


def test_the_tolerance_widens_as_the_span_shortens():
    """One miscounted breath matters more over two minutes than over an hour."""
    from prisma_vent.validate import rate_tolerance_per_min

    assert rate_tolerance_per_min(2.0) == pytest.approx(1.0)     # 0.5 + 1/2
    assert rate_tolerance_per_min(60.0) == pytest.approx(0.5167, abs=1e-4)
    assert rate_tolerance_per_min(2.0) > rate_tolerance_per_min(60.0)


def test_the_minimum_span_is_where_the_two_error_terms_are_equal():
    """Two minutes is derived, not chosen: 1/T <= 0.5 gives T >= 2."""
    from prisma_vent.validate import (
        DEVICE_RATE_ACCURACY_PER_MIN,
        MIN_SECONDS_FOR_RATE,
        rate_tolerance_per_min,
    )

    minimum_minutes = MIN_SECONDS_FOR_RATE / 60
    counting_term = 1.0 / minimum_minutes
    assert counting_term == pytest.approx(DEVICE_RATE_ACCURACY_PER_MIN)
    assert rate_tolerance_per_min(minimum_minutes) == pytest.approx(
        2 * DEVICE_RATE_ACCURACY_PER_MIN
    )


def test_a_zero_or_negative_span_is_refused():
    from prisma_vent.validate import rate_tolerance_per_min

    for span in (0.0, -1.0):
        with pytest.raises(ValueError, match="span must be positive"):
            rate_tolerance_per_min(span)


def test_a_difference_just_inside_the_bound_passes(tmp_path):
    """Boundary fixture, built from synthetic data alone."""
    status, _ = run(
        _archive(tmp_path, breaths_per_minute=15, reported_rate=15.0 + 0.4)
    )
    assert status == EXIT_OK


def test_a_difference_above_instrument_accuracy_is_reported_not_asserted(tmp_path):
    """Between the two bounds: rate varies within a session, and that is not a
    decoding fault. The difference is printed; the run still succeeds."""
    status, text = run(
        _archive(tmp_path, breaths_per_minute=15, reported_rate=15.0 + 5.0)
    )
    assert status == EXIT_OK
    assert "Reported, not asserted" in text


def test_the_gross_disagreement_factor_is_gone(tmp_path):
    """It was a threshold fitted to evidence that cannot be published.

    The factor of two was justified by the ratios some misparses happened to
    produce during development, plus an unsourced claim about what physiology
    does not reach. Neither survives as a public derivation, so the constant
    was withdrawn rather than reworded — and no rate difference, however
    large, fails this run any more.
    """
    import prisma_vent.validate as validate

    assert not hasattr(validate, "GROSS_DISAGREEMENT_FACTOR")
    for reported in (28.0, 32.0, 60.0, 1.0):
        status, _ = run(
            _archive(tmp_path / str(reported), breaths_per_minute=15,
                     reported_rate=reported)
        )
        assert status == EXIT_OK


def test_the_report_says_on_demand_ventilation_looks_like_this_too(tmp_path):
    """The check cannot tell a misparse from breaths taken on demand.

    That is the second reason it reports rather than judges, and the reader
    has to be told, or a large difference reads as a decoding fault.
    """
    _, text = run(_archive(tmp_path, breaths_per_minute=15, reported_rate=45.0))
    assert "on-demand ventilation" in text


def test_volume_ratio_is_measured_but_never_asserted(tmp_path):
    """An expected one-sided relationship that did not hold.

    The integral was expected to sit at or above the reported volume, since
    the report is leak-compensated and the integral is not. It does not
    reliably do so, and the competing explanations cannot be told apart from
    the data, so nothing is concluded from the ratio in either direction.
    """
    low, _ = run(_archive(tmp_path, inspiratory_flow=10.0, reported_tidal=900.0))
    high, _ = run(_archive(tmp_path, inspiratory_flow=30.0, reported_tidal=50.0))
    assert low == EXIT_OK and high == EXIT_OK


def test_volume_ratio_appears_in_the_report(tmp_path):
    paths = [_archive(tmp_path / str(i)) for i in range(3)]
    _, text = run(*paths)
    assert "integrated flow over reported tidal volume" in text
    assert "no bound is asserted" in text


def test_duration_discrepancy_is_reported_without_a_bound(tmp_path):
    _, text = run(_archive(tmp_path))
    assert "reported, not bounded" in text
    assert "chosen to pass" in text


def test_report_mode_never_fails_on_a_finding(tmp_path):
    """Survey mode surveys; it does not judge.

    The finding here is a plausibility failure, which is one of the two checks
    that still assert — a breath-rate difference no longer fails anything, so
    it could not demonstrate this.
    """
    path = _mis_scaled_archive(tmp_path, 650.0)
    status, text = run("--report", path)
    assert status == EXIT_OK
    assert "reported, not treated as failure" in text


def test_unreadable_archive_is_a_structural_failure(tmp_path, capsys):
    bad = tmp_path / "0123_2020-03-04.zip"
    bad.write_bytes(b"not a zip")
    status, _ = run(bad)
    assert status == EXIT_STRUCTURAL
    assert "not a readable ZIP" in capsys.readouterr().err


def test_missing_channels_are_not_comparable_rather_than_passing(tmp_path):
    """Absence is not agreement.

    A firmware need not write every channel, but a check that could not run
    must not be reported as one that ran cleanly.
    """
    path = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE)
    status, text = run(path)
    assert status == EXIT_OK  # documented: not comparable does not fail
    assert "not comparable" in text
    assert "absent from this file" in text
    assert "No cross-channel check could be run." in text
    assert "No cross-channel disagreement" not in text
    assert "had no cross-channel check run at all" in text


def test_a_session_too_short_to_measure_is_not_compared(tmp_path):
    """A rate over a handful of breaths says nothing about the decode.

    A short session is common and is not a decode failure; reporting it as
    decode failures would be the check misfiring, not working.
    """
    path = _archive(
        tmp_path,
        n_records=int(MIN_SECONDS_FOR_RATE) - 1,
        breaths_per_minute=15,
        reported_rate=30.0,   # a disagreement it must decline to judge
    )
    status, text = run(path)
    assert status == EXIT_OK
    assert "breath rate not comparable" in text
    assert f"under the {MIN_SECONDS_FOR_RATE:.0f} s" in text
    # Padding-independent: the column width follows the longest check name,
    # and asserting it here would make adding a check a test failure.
    assert re.search(r"breath rate\s+passed 0", text)


def test_a_session_without_regular_breathing_is_not_compared(tmp_path):
    """No pressure, no flow, a phase channel stuck in one state.

    Mouthpiece ventilation produces this shape — breaths are taken on demand
    rather than delivered on a cycle — and so does a device left running with
    nothing connected. Nothing in this channel separates the two, so the
    message must not assert which of them it was. The fixture is invented.
    """
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml

    n = 400
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    signals = [
        SyntheticSignal("Breath Phase", "", 0.0, 1.0, 1, 2, 10, 1, data=[2] * (n * 10)),
        SyntheticSignal("Patient Flow", "l/min", -120.0, 250.0, -1200, 2500, 10, 2,
                        data=[0] * (n * 10)),
        SyntheticSignal("Frequency", "1/min", 0.0, 80.0, 0, 800, 1, 2, data=[180] * n),
        SyntheticSignal("Tidal Volume", "ml", 0.0, 3000.0, 0, 30000, 1, 2, data=[0] * n),
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))

    status, text = run(path)
    assert status == EXIT_OK
    assert "no regular breathing rate to count" in text
    # The cause is not asserted: two very different situations look alike here.
    assert "Mouthpiece ventilation" in text
    assert "cannot be told from this channel alone" in text


# --------------------------------------------------------------------------
# Record duration is read, never assumed
# --------------------------------------------------------------------------


def test_breath_rate_is_correct_with_two_second_records(tmp_path):
    """The same 10 Hz signal, written as two-second records.

    A decoder treating samples_per_record as a rate in hertz would compute
    half the true breathing rate here and report a disagreement that is
    entirely its own doing.
    """
    path = _archive(
        tmp_path, n_records=200, record_duration="2",
        breaths_per_minute=15, reported_rate=15.0,
    )
    status, text = run(path)
    assert status == EXIT_OK
    assert re.search(r"breath rate\s+passed 0\s+failed 0\s+reported 1", text)
    session = _check(path).sessions[0]
    # The rate the check counted, which is what a halved sample interval would
    # have got wrong: 15/min against a reported 15/min is a ratio near one.
    assert session.breath.ratio == pytest.approx(1.0, rel=0.05)


def test_volume_integration_is_correct_with_two_second_records(tmp_path):
    """Integration must use the real sample interval, not one record.

    With two-second records each sample still covers a tenth of a second;
    assuming otherwise doubles every integrated volume.
    """
    one = _archive(
        tmp_path / "a", n_records=400, record_duration="1",
        inspiratory_flow=30.0, reported_tidal=200.0,
    )
    two = _archive(
        tmp_path / "b", n_records=200, record_duration="2",
        inspiratory_flow=30.0, reported_tidal=200.0,
    )
    from prisma_vent.validate import _check

    ratio_one = _check(one).sessions[0].volume.ratio
    ratio_two = _check(two).sessions[0].volume.ratio
    assert ratio_one is not None and ratio_two is not None
    # Same physical signal, same answer, whatever the record length.
    assert ratio_two == pytest.approx(ratio_one, rel=0.02)


def test_minimum_length_is_measured_in_seconds_not_records(tmp_path):
    """200 records of two seconds is 400 s, which is over the threshold.

    Counting records instead would reject it as too short.
    """
    path = _archive(tmp_path, n_records=200, record_duration="2")

    assert _check(path).sessions[0].breath.status is CheckStatus.REPORTED


# --------------------------------------------------------------------------
# Range violations fail normal validation
# --------------------------------------------------------------------------


def _archive_with_range_violation(tmp_path):
    """A channel carrying a sample above its own declared digital maximum."""
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml

    tmp_path.mkdir(parents=True, exist_ok=True)
    n = 400
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    data = [50] * n
    data[7] = 200  # declared maximum is 100
    signals = [
        SyntheticSignal("Odd Channel", "%", 0.0, 100.0, 0, 100, 1, 2, data=data)
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))
    return path


def test_range_violation_fails_normal_validation(tmp_path):
    """Reporting a violation and then exiting zero would pass the problem.

    A sample outside the range its own header declares is the signature of a
    misaligned decode.
    """
    status, text = run(_archive_with_range_violation(tmp_path))
    assert status == EXIT_STRUCTURAL
    assert "RANGE VIOLATION" in text
    assert "outside their declared digital range" in text


def test_range_violation_is_distinguished_from_disagreement(tmp_path):
    _, text = run(_archive_with_range_violation(tmp_path))
    assert "RANGE VIOLATION" in text
    # Not lumped in with the cross-channel count, which is a different fault.
    assert "cross-channel disagreement(s)" not in text


def test_no_unqualified_success_when_violations_exist(tmp_path):
    _, text = run(_archive_with_range_violation(tmp_path))
    assert "No cross-channel disagreement among the checks that assert." not in text


def test_survey_mode_reports_range_violations_without_failing(tmp_path):
    """Documented behaviour: --report judges nothing it surveyed."""
    status, text = run("--report", _archive_with_range_violation(tmp_path))
    assert status == EXIT_OK
    assert "RANGE VIOLATION" in text
    assert "reported, not treated as failure" in text


def test_survey_mode_still_fails_on_an_unreadable_archive(tmp_path, capsys):
    """There was nothing to survey, so this is not a survey result."""
    bad = tmp_path / "0123_2020-03-04.zip"
    bad.write_bytes(b"not a zip")
    status, _ = run("--report", bad)
    assert status == EXIT_STRUCTURAL
    assert "not a readable ZIP" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The two checks carry independent status
# --------------------------------------------------------------------------


def test_checks_have_separate_status(tmp_path):
    """One check can be impossible while the other succeeds.

    Here the rate channel is absent, so the breath comparison cannot run —
    but flow, volume and phase are all present, so the volume ratio still is
    measured.
    """
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml
    from prisma_vent.validate import CheckStatus, _check

    tmp_path.mkdir(parents=True, exist_ok=True)
    n = 400
    period, half = 40, 20
    phase, flow = [], []
    for sample in range(n * 10):
        inspiring = (sample % period) < half
        phase.append(2 if inspiring else 1)
        flow.append(300 if inspiring else -300)
    signals = [
        SyntheticSignal("Breath Phase", "", 0.0, 1.0, 1, 2, 10, 1, data=phase),
        SyntheticSignal("Patient Flow", "l/min", -120.0, 250.0, -1200, 2500, 10, 2,
                        data=flow),
        SyntheticSignal("Tidal Volume", "ml", 0.0, 3000.0, 0, 30000, 1, 2,
                        data=[2000] * n),
    ]
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))

    session = _check(path).sessions[0]
    assert session.breath.status is CheckStatus.NOT_COMPARABLE
    assert "Frequency" in session.breath.detail
    assert session.volume.status is CheckStatus.REPORTED
    assert session.volume.ratio is not None
    # A check that only reports still ran, which is what this flag is about.
    assert session.any_check_ran


def test_a_session_with_no_checks_is_counted_as_such(tmp_path):
    path = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE)
    from prisma_vent.validate import _check

    session = _check(path).sessions[0]
    assert not session.any_check_ran
    assert session.breath.status is CheckStatus.NOT_COMPARABLE
    assert session.volume.status is CheckStatus.NOT_COMPARABLE


# --------------------------------------------------------------------------
# The one check that is not self-referential
# --------------------------------------------------------------------------


def _mis_scaled_archive(tmp_path, physical_max: float):
    """A file that agrees with itself perfectly and is nonetheless wrong.

    Every declared range is consistent, every sample sits inside it, and the
    record layout is sound — so every existing check passes. Only a bound from
    outside the file can see that the pressures are impossible.
    """
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml

    tmp_path.mkdir(parents=True, exist_ok=True)
    n = 400
    # Half of full scale, so a correctly scaled file stays well inside the
    # bound and the mis-scaled one lands well outside it. Neither case sits
    # near the threshold, because a test that passes by a hair is not evidence.
    signals = [
        SyntheticSignal(
            "Airway Pressure", "hPa", 0.0, physical_max, 0, 650, 10, 2,
            data=[325] * (n * 10),
        )
    ]
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))
    return path


def test_a_self_consistent_file_with_impossible_values_fails(tmp_path):
    """Scale off by a factor of ten: internally flawless, physically absurd."""
    status, text = run(_mis_scaled_archive(tmp_path, 650.0))
    assert status == EXIT_STRUCTURAL
    assert "outside the published" in text
    assert "read wrongly but consistently" in text


def test_the_same_file_correctly_scaled_passes(tmp_path):
    status, text = run(_mis_scaled_archive(tmp_path, 65.0))
    assert status == EXIT_OK
    assert "outside the published" not in text


def test_the_declared_range_check_does_not_catch_it(tmp_path):
    """Shows why the external bound is needed rather than merely nice.

    Every sample sits inside the range its own header declares, so the
    existing range check reports nothing at all.
    """
    from prisma_vent.validate import _check

    report = _check(_mis_scaled_archive(tmp_path, 650.0))
    session = report.sessions[0]
    assert session.violations.total_seen == 0        # nothing self-inconsistent
    assert session.plausibility.status is CheckStatus.FAILED


def test_a_bound_narrower_than_the_header_says_so(tmp_path):
    """The diagnostic that would have caught this module's own first error.

    A settable range read as a measurement limit shows up as a bound tighter
    than the device's own declared range. When that combination produces a
    failure, the report must point at the bound rather than at the decoder.
    """
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml
    from prisma_vent.validate import _check

    tmp_path.mkdir(parents=True, exist_ok=True)
    n = 20
    # SpO2 is bounded at 100 % by arithmetic; a header declaring 0..200 is
    # wider than the bound, which is the situation being described.
    signals = [
        SyntheticSignal(
            "SpO2 Value", "%", 0.0, 200.0, 0, 200, 1, 2, data=[150] * n
        )
    ]
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))

    session = _check(path).sessions[0]
    assert session.plausibility.status is CheckStatus.FAILED
    assert "narrower than the range the file's own header declares" in (
        session.plausibility.detail
    )


def test_the_failure_names_which_kind_of_figure_it_used(tmp_path):
    """Settable, capability and arithmetic fail differently and must be told apart."""
    from prisma_vent.device_limits import bound_for, SETTABLE, CAPABILITY, ARITHMETIC

    _, text = run(_mis_scaled_archive(tmp_path, 650.0))
    assert f"[{bound_for('Airway Pressure').basis}]" in text
    assert bound_for("Airway Pressure").basis == CAPABILITY
    assert bound_for("IPAPsoll").basis == SETTABLE
    assert bound_for("SpO2 Value").basis == ARITHMETIC


def test_the_physically_impossible_basis_no_longer_exists(tmp_path):
    """It rested on assertions about the body that no cited source supported."""
    import prisma_vent.device_limits as limits

    assert not hasattr(limits, "IMPOSSIBLE")
    assert all(b.basis != "physically impossible" for b in limits.BOUNDS.values())


def test_a_channel_without_a_documented_bound_is_not_invented(tmp_path):
    """Absence of a bound means not checked, never unbounded."""
    from prisma_vent.device_limits import bound_for

    assert bound_for("Airway Pressure") is not None
    assert bound_for("ARP Volume Dbg") is None


def test_a_file_with_no_bounded_channel_is_not_comparable(tmp_path):
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml
    from prisma_vent.validate import _check

    tmp_path.mkdir(parents=True, exist_ok=True)
    n = 400
    signals = [
        SyntheticSignal("Nameless Channel", "", 0.0, 1.0, 0, 1, 1, 1, data=[0] * n)
    ]
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))

    session = _check(path).sessions[0]
    assert session.plausibility.status is CheckStatus.NOT_COMPARABLE
    assert "no channel in this file has a published physical bound" in (
        session.plausibility.detail
    )


def test_bounds_carry_the_reason_they_were_chosen(tmp_path):
    """So a later reader can judge whether a failure means the decode or the bound."""
    from prisma_vent.device_limits import BOUNDS, SETTABLE, CAPABILITY, ARITHMETIC

    assert all(b.source for b in BOUNDS.values())
    assert all(b.basis in (SETTABLE, CAPABILITY, ARITHMETIC) for b in BOUNDS.values())
    for bound in BOUNDS.values():
        assert bound.minimum is not None or bound.maximum is not None
        if bound.minimum is not None and bound.maximum is not None:
            assert bound.minimum < bound.maximum


def test_no_measured_channel_is_bounded_by_a_settable_range(tmp_path):
    """A settable range bounds what a clinician may dial in, and nothing else.

    Applying one to a channel that reports a measurement is how this module
    first failed, so the only channels carrying that basis are the ones that
    report settings.
    """
    from prisma_vent.device_limits import BOUNDS, SETTABLE

    settable = {k for k, v in BOUNDS.items() if v.basis == SETTABLE}
    assert settable == {"IPAPsoll", "EPAPsoll", "Trigger Level"}


def test_volume_and_flow_carry_no_bound_at_all(tmp_path):
    """A lower bound cannot be turned into a ceiling, so there is no ceiling.

    The manual gives maximum flow as *above* 220 l/min — a guaranteed minimum
    capability. The withdrawn bounds were 400 l/min for flow and 15 l for a
    single breath, the latter computed as that same lower bound times the
    longest settable inspiratory time. Neither is an upper bound on anything,
    so both are gone and the channels are simply not checked.
    """
    from prisma_vent.device_limits import BOUNDS, WITHDRAWN_BOUNDS, bound_for

    for label in ("Patient Flow", "Leakage Flow", "TotalLeakage",
                  "Tidal Volume", "Online Volume", "Minute Volume"):
        assert bound_for(label) is None
        assert label in WITHDRAWN_BOUNDS

    # And nothing reintroduced them under another name: no bound anywhere in
    # the table is expressed in a flow or volume unit.
    assert not any(b.unit in ("l/min", "ml", "l") for b in BOUNDS.values())


def test_rate_and_pulse_ceilings_are_withdrawn(tmp_path):
    """They rested on an assertion about the human body with no cited source."""
    from prisma_vent.device_limits import WITHDRAWN_BOUNDS, bound_for

    assert bound_for("Frequency") is None
    assert bound_for("Pulse Rate") is None
    assert "Frequency" in WITHDRAWN_BOUNDS and "Pulse Rate" in WITHDRAWN_BOUNDS


def test_the_internal_pressure_sensors_are_not_bounded_by_analogy(tmp_path):
    """The 60 hPa figure is published for the pressure delivered to the patient.

    Nothing published says what an upstream valve-control sensor may read, so
    borrowing the figure for those channels was an inference, not a citation.
    """
    from prisma_vent.device_limits import bound_for

    assert bound_for("Airway Pressure") is not None
    for label in ("P main 2 Dbg", "P valve ctrl Dbg", "P valve air Dbg"):
        assert bound_for(label) is None


def test_every_retained_bound_is_one_sided_except_the_percentages(tmp_path):
    """Every floor in the table came from describing a recording, so none stayed.

    The percentages are the exception: their unit closes both ends by
    definition, which needs no recording and no manual.
    """
    from prisma_vent.device_limits import ARITHMETIC, BOUNDS

    for label, bound in BOUNDS.items():
        if bound.basis == ARITHMETIC:
            assert (bound.minimum, bound.maximum) == (0.0, 100.0), label
            assert bound.unit == "%"
        else:
            assert bound.minimum is None, label
            assert bound.maximum is not None, label


def test_an_open_side_is_not_checked(tmp_path):
    """A one-sided bound must not silently reject on the side it does not bound."""
    from prisma_vent.device_limits import bound_for

    pressure = bound_for("Airway Pressure")
    assert pressure.minimum is None
    # Far below anything a blower produces, and deliberately not a failure:
    # nothing published states how far below zero this sensor may read.
    assert not pressure.exceeded_by(-500.0, 10.0)
    assert pressure.exceeded_by(0.0, 60.001)
    assert not pressure.exceeded_by(0.0, 60.0)
    assert pressure.describe() == "unbounded..60"


@pytest.mark.parametrize(
    "label, maximum",
    [
        ("Airway Pressure", 60.0),
        ("IPAPsoll", 50.0),
        ("EPAPsoll", 25.0),
        ("Trigger Level", 8.0),
        ("SpO2 Value", 100.0),
        ("SpO2 Sig. Qual.", 100.0),
        ("Part Inspiration", 100.0),
        ("Part Spont Insp.", 100.0),
        ("Part Spont Exp.", 100.0),
    ],
)
def test_each_retained_maximum_is_the_cited_figure_and_is_inclusive(label, maximum):
    """Boundary behaviour for every bound the table still carries.

    The published figure itself is inside the bound; anything above it is out.
    Stated per channel so a later edit to one number cannot pass unnoticed.
    """
    from prisma_vent.device_limits import bound_for

    bound = bound_for(label)
    assert bound.maximum == maximum
    assert not bound.exceeded_by(0.0, maximum)
    assert bound.exceeded_by(0.0, maximum + 0.001)


def test_a_percentage_below_zero_is_out_of_bounds():
    """The arithmetic bound closes both ends, unlike every other bound here."""
    from prisma_vent.device_limits import bound_for

    spo2 = bound_for("SpO2 Value")
    assert spo2.exceeded_by(-0.001, 50.0)
    assert not spo2.exceeded_by(0.0, 100.0)
    assert spo2.describe() == "0..100"


def test_a_bound_must_constrain_something():
    """A PhysicalBound open at both ends would be a check that never fires."""
    from prisma_vent.device_limits import CAPABILITY, PhysicalBound

    with pytest.raises(ValueError, match="must constrain something"):
        PhysicalBound("Nothing", None, None, "hPa", CAPABILITY, "no source")
    with pytest.raises(ValueError, match="minimum is not below maximum"):
        PhysicalBound("Inverted", 10.0, 1.0, "hPa", CAPABILITY, "no source")


def test_every_withdrawn_bound_says_why(tmp_path):
    """The temptation to reinstate one is real; the answer lives with the table."""
    from prisma_vent.device_limits import BOUNDS, WITHDRAWN_BOUNDS

    assert WITHDRAWN_BOUNDS
    assert all(reason.strip() for reason in WITHDRAWN_BOUNDS.values())
    # Nothing is both withdrawn and in force.
    assert not set(WITHDRAWN_BOUNDS) & set(BOUNDS)


# --------------------------------------------------------------------------
# Against the device's own long-term record
# --------------------------------------------------------------------------


def _with_long_term(tmp_path, session_seconds, record_specs):
    """An archive whose session and long-term record can be made to disagree."""
    from synthetic import build_day_archive, build_statistic, build_usage_record
    from prisma_vent.statistic import EPOCH_OFFSET

    start = datetime(2020, 3, 4, 22, 0, 0)
    counter = int((start - datetime(1970, 1, 1) - EPOCH_OFFSET).total_seconds())
    return build_day_archive(
        tmp_path,
        archive_date=date(2020, 3, 4),
        sessions=[(1, start, session_seconds)],
        extra_members={
            "statistic.proto": build_statistic(
                records=[
                    build_usage_record(
                        timestamp=counter + offset, duration=minutes
                    )
                    for offset, minutes in record_specs
                ],
                therapy_total=1_000_000,
            )
        },
    )


def test_a_session_agreeing_with_the_long_term_record_passes(tmp_path):
    archive = _with_long_term(tmp_path, 3600, [(0, 60)])
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.PASSED


def test_a_few_seconds_of_skew_still_matches(tmp_path):
    """The two timestamps differ by seconds; exact equality would be wrong."""
    archive = _with_long_term(tmp_path, 3600, [(4, 60)])
    assert _check(archive).sessions[0].long_term.status is CheckStatus.PASSED


def test_the_device_truncating_is_not_a_disagreement(tmp_path):
    """3630 s is 60.5 min, which the device stores as 60, not 61."""
    archive = _with_long_term(tmp_path, 3630, [(0, 60)])
    assert _check(archive).sessions[0].long_term.status is CheckStatus.PASSED


def test_two_of_the_devices_outputs_disagreeing_is_a_failure(tmp_path):
    archive = _with_long_term(tmp_path, 3600, [(0, 45)])
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.FAILED
    assert "long-term record says 45" in session.long_term.detail


def test_a_long_session_with_no_entry_at_all_is_a_failure(tmp_path):
    archive = _with_long_term(tmp_path, 3600, [(100_000, 60)])
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.FAILED
    assert "holds no entry within" in session.long_term.detail


def test_a_sub_minute_session_leaving_no_entry_is_expected(tmp_path):
    """It truncates to zero minutes, so the device stores nothing."""
    archive = _with_long_term(tmp_path, 30, [(100_000, 60)])
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.NOT_COMPARABLE
    assert "leaves no long-term record" in session.long_term.detail


def test_an_ambiguous_match_is_not_comparable_rather_than_guessed(tmp_path):
    archive = _with_long_term(tmp_path, 3600, [(0, 60), (5, 60)])
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.NOT_COMPARABLE
    assert "none can be attributed" in session.long_term.detail


def test_an_archive_without_the_member_is_not_comparable(tmp_path):
    from synthetic import build_day_archive

    archive = build_day_archive(
        tmp_path,
        archive_date=date(2020, 3, 4),
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 3600)],
    )
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.NOT_COMPARABLE
    assert "no readable long-term record" in session.long_term.detail


def test_a_caller_that_forgets_the_index_still_gets_the_check(tmp_path):
    """The exported validation report reported it as never attempted once."""
    from prisma_vent.archive import open_day_archive
    from prisma_vent.validate import _check_session

    archive = _with_long_term(tmp_path, 3600, [(0, 60)])
    with open_day_archive(archive) as opened:
        report = _check_session(opened, opened.sessions[0])
    assert report.long_term.status is CheckStatus.PASSED


def _diverging(tmp_path, record_seconds, xml_seconds, banked_minutes):
    """A session whose two durations straddle a whole-minute boundary."""
    from synthetic import build_day_archive, build_statistic, build_usage_record
    from prisma_vent.statistic import EPOCH_OFFSET

    start = datetime(2020, 3, 4, 22, 0, 0)
    counter = int((start - datetime(1970, 1, 1) - EPOCH_OFFSET).total_seconds())
    records = (
        []
        if banked_minutes is None
        else [build_usage_record(timestamp=counter, duration=banked_minutes)]
    )
    return build_day_archive(
        tmp_path,
        archive_date=date(2020, 3, 4),
        sessions=[(1, start, record_seconds, xml_seconds)],
        extra_members={
            "statistic.proto": build_statistic(
                records=records, therapy_total=1_000_000
            )
        },
    )


def test_the_xml_span_decides_the_long_term_check(tmp_path):
    """Which of the two spans the device banks was settled by measurement.

    Across the sessions carrying a long-term entry, the truncated XML span
    matches every one while the truncated record-derived span does not. A
    review proposed the record count on the grounds that it is the more
    independent quantity — it is, and the device does not use it. Asserting
    against it fails sessions whose decode is correct.

    Here the record span is 119 s (1 minute) and the XML span 121 s (2). The
    device's entry says 2, and that must pass.
    """
    passing = _diverging(tmp_path / "a", 119, 121, banked_minutes=2)
    assert _check(passing).sessions[0].long_term.status is CheckStatus.PASSED

    failing = _diverging(tmp_path / "b", 119, 121, banked_minutes=1)
    session = _check(failing).sessions[0]
    assert session.long_term.status is CheckStatus.FAILED
    assert "truncates to 2" in session.long_term.detail


def test_the_floor_is_measured_against_the_span_the_device_banks(tmp_path):
    """A 30 s session leaves no entry, whatever the record count says."""
    archive = _diverging(tmp_path, 90, 30, banked_minutes=None)
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.NOT_COMPARABLE
    assert "spans 30 s" in session.long_term.detail


def test_a_missing_entry_for_a_banked_minute_still_fails(tmp_path):
    """The converse: XML above the floor, records below it."""
    archive = _diverging(tmp_path, 30, 90, banked_minutes=None)
    session = _check(archive).sessions[0]
    assert session.long_term.status is CheckStatus.FAILED
    assert "spans 1.50 min" in session.long_term.detail


def test_the_xml_span_is_still_reported_separately(tmp_path):
    """It is not this check's business, but it is not discarded either."""
    archive = _diverging(tmp_path, 119, 121, banked_minutes=1)
    session = _check(archive).sessions[0]
    assert session.duration_discrepancy_s == pytest.approx(2.0)


# --------------------------------------------------------------------------
# The two preconditions on the breath-rate report
#
# Neither can fail a run; both decide whether a figure is produced at all.
# Their derivations are in docs/thresholds.md, and these tests pin the numbers
# so a later edit to one cannot pass unnoticed.
# --------------------------------------------------------------------------


def test_the_duty_cycle_floor_is_derived_from_the_shortest_settable_ti():
    """0.2 s over the longest period two edges can span, rounded down.

    The manual gives 0.5 s for Ti/Ti max but 0.2 s for Ti min, on the same
    page of the same table. An earlier value of 0.01 used the 0.5 s figure and
    a 60 s period, which put the floor above what the device can produce — so
    a session the device could legitimately record would have been refused.
    """
    from prisma_vent.validate import MIN_PHASE_DUTY_CYCLE, MIN_SECONDS_FOR_RATE

    shortest_ti_seconds = 0.2          # IFU 11.1.1, p. 49
    smallest_producible = shortest_ti_seconds / MIN_SECONDS_FOR_RATE
    assert smallest_producible == pytest.approx(0.001667, abs=1e-6)
    # Rounded down, because a floor above the device's own minimum refuses
    # sessions that are perfectly correct.
    assert MIN_PHASE_DUTY_CYCLE == 0.001
    assert MIN_PHASE_DUTY_CYCLE < smallest_producible


def _phase_only_archive(tmp_path, duty_samples: int, total: int):
    """A phase channel in its upper state for exactly *duty_samples* samples."""
    import zipfile
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml

    tmp_path.mkdir(parents=True, exist_ok=True)
    n = total // 10
    phase = [1] * (total - duty_samples) + [2] * duty_samples
    signals = [
        SyntheticSignal("Breath Phase", "", 0.0, 1.0, 1, 2, 10, 1, data=phase),
        SyntheticSignal(
            "Frequency", "1/min", 0.0, 80.0, 0, 800, 1, 2, data=[150] * n
        ),
    ]
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n))
    return path


def test_a_duty_cycle_exactly_at_the_floor_is_accepted(tmp_path):
    """Boundary behaviour, stated rather than left to a fixture's luck."""
    from prisma_vent.validate import MIN_PHASE_DUTY_CYCLE

    total = 10_000
    at_the_floor = int(total * MIN_PHASE_DUTY_CYCLE)     # exactly 0.001
    session = _check(_phase_only_archive(tmp_path, at_the_floor, total)).sessions[0]
    assert session.breath.status is not CheckStatus.NOT_COMPARABLE or (
        "alternates" not in (session.breath.detail or "")
    )


def test_a_duty_cycle_below_the_floor_is_not_compared(tmp_path):
    """There is no periodic signal to count, so no figure is produced."""
    total = 10_000
    session = _check(_phase_only_archive(tmp_path, 5, total)).sessions[0]
    assert session.breath.status is CheckStatus.NOT_COMPARABLE
    assert "alternates for only" in session.breath.detail


def test_a_channel_stuck_in_one_state_is_not_compared(tmp_path):
    """Duty cycle zero: the channel never changed state at all."""
    total = 10_000
    session = _check(_phase_only_archive(tmp_path, 0, total)).sessions[0]
    assert session.breath.status is CheckStatus.NOT_COMPARABLE


def test_the_precondition_message_refuses_to_say_which_it_was(tmp_path):
    """On-demand ventilation and an idle machine look identical here."""
    session = _check(_phase_only_archive(tmp_path, 5, 10_000)).sessions[0]
    detail = session.breath.detail
    assert "Mouthpiece ventilation" in detail
    assert "cannot be told from" in detail


# --------------------------------------------------------------------------
# Duplicate long-term timestamps must not be silently collapsed
#
# `_long_term_index` was a `{record.start: record}` comprehension, so two
# records sharing one instant became one — whichever came last. The same file
# then passed or failed depending only on the order its records were written
# in, which is not a property of the data.
# --------------------------------------------------------------------------


def _duplicate_timestamp_archive(tmp_path, minutes_in_order):
    """One session, and two long-term records at the *same* instant.

    *minutes_in_order* gives the durations in the order they are written, so a
    test can present the identical pair both ways round.
    """
    from synthetic import build_day_archive, build_statistic, build_usage_record
    from prisma_vent.statistic import EPOCH_OFFSET

    tmp_path.mkdir(parents=True, exist_ok=True)
    start = datetime(2020, 3, 4, 22, 0, 0)
    counter = int((start - datetime(1970, 1, 1) - EPOCH_OFFSET).total_seconds())
    return build_day_archive(
        tmp_path,
        archive_date=date(2020, 3, 4),
        # 45 minutes of session, so a 45-minute record would agree and a
        # 60-minute one would not. Which is exactly the pair being fed in.
        sessions=[(1, start, 45 * 60)],
        extra_members={
            "statistic.proto": build_statistic(
                records=[
                    build_usage_record(timestamp=counter, duration=minutes)
                    for minutes in minutes_in_order
                ],
                therapy_total=1_000_000,
            )
        },
    )


def test_two_records_at_one_instant_are_not_collapsed(tmp_path):
    """Both survive into the index, so both are visible as candidates."""
    from prisma_vent.validate import _long_term_index
    from prisma_vent.archive import open_day_archive

    path = _duplicate_timestamp_archive(tmp_path, [45, 60])
    with open_day_archive(path) as archive:
        index = _long_term_index(archive)

    assert len(index) == 1, "one timestamp"
    (records,) = index.values()
    assert [r.duration_minutes for r in records] == [45, 60], "both records kept"


@pytest.mark.parametrize(
    "order", [[45, 60], [60, 45]], ids=["agreeing-first", "disagreeing-first"]
)
def test_a_duplicated_timestamp_is_ambiguous_in_either_order(tmp_path, order):
    """The defect: this used to be `passed` one way round and `failed` the other.

    Neither is an honest answer. The device wrote two records at one instant
    and nothing in the file says which belongs to this session, so the check
    refuses instead of picking.
    """
    session = _check(_duplicate_timestamp_archive(tmp_path / str(order), order)).sessions[0]
    assert session.long_term.status is CheckStatus.NOT_COMPARABLE
    assert "2 long-term records fall within" in session.long_term.detail


def test_the_result_does_not_depend_on_record_order(tmp_path):
    """Stated as its own test, because order-independence is the actual fix."""
    forwards = _check(_duplicate_timestamp_archive(tmp_path / "f", [45, 60])).sessions[0]
    backwards = _check(_duplicate_timestamp_archive(tmp_path / "b", [60, 45])).sessions[0]

    assert forwards.long_term.status is backwards.long_term.status
    assert forwards.long_term.detail == backwards.long_term.detail


def test_identical_durations_at_one_instant_are_still_ambiguous(tmp_path):
    """Even agreeing duplicates are refused: the ambiguity is structural.

    Resolving it because the two happen to agree would mean the check quietly
    changes its mind about how many records it is allowed to see, which is how
    a rule stops being a rule.
    """
    session = _check(_duplicate_timestamp_archive(tmp_path, [45, 45])).sessions[0]
    assert session.long_term.status is CheckStatus.NOT_COMPARABLE


def test_a_single_record_at_a_timestamp_still_compares_normally(tmp_path):
    """The grouping must not break the ordinary case."""
    session = _check(_duplicate_timestamp_archive(tmp_path, [45])).sessions[0]
    assert session.long_term.status is CheckStatus.PASSED


# --------------------------------------------------------------------------
# Target volume
#
# The only check here that holds a *setting* against a *measurement*. It is
# always reported: what separates a delivered volume from its target is what
# the device is regulating, and a bound on that would be a clinical claim.
# --------------------------------------------------------------------------


def _archive_with_targets(
    tmp_path,
    targets,
    *,
    enabled=("1", "1", "0"),
    targeting=True,
    sessions=1,
    tidal_zero_samples=0,
    positions=None,
):
    """An archive whose programme blocks carry the given volume targets."""
    import zipfile

    from synthetic import (
        REGISTERED_PARAMETER_IDS,
        _parameter_map_xml,
        session_event_xml,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f'<parameter time="+0012:00:00.000" id="{i}" value="{v}"/>'
        for i, v in [
            (1, "0"),
            *((6 + n, flag) for n, flag in enumerate(enabled)),
        ]
    )
    for position, target in zip(
        positions if positions is not None else range(len(targets)),
        targets,
        strict=True,
    ):
        rows += (
            f'<parameter time="+0012:00:00.000" id="111" value="{target}" '
            f'program="{position}"/>'
            f'<parameter time="+0012:00:00.000" id="69" value="1600" '
            f'program="{position}"/>'
        )
    log = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<log version="1" date="2020-03-04">'
        f'<config version="6.3.0"/>{rows}</log>'
    ).encode("utf-8")
    entries = (
        (1, "ActiveProgram"),
        (6, "ProgramEnabled1"),
        (7, "ProgramEnabled2"),
        (8, "ProgramEnabled3"),
        *REGISTERED_PARAMETER_IDS,
    )

    path = tmp_path / "0123_2020-03-04.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for number in range(1, sessions + 1):
            start = datetime(2020, 3, 4, 20 + number, 0, 0)
            zf.writestr(
                f"{number:04d}.wmedf",
                _breathing_session(
                    400,
                    targeting=targeting,
                    tidal_zero_samples=tidal_zero_samples,
                    start=start,
                ),
            )
            zf.writestr(f"event_{number:04d}.xml", session_event_xml(start, 400))
        zf.writestr("parameter.xml", log)
        zf.writestr("parametersmap.xml", _parameter_map_xml(entries=entries))
    return path


def test_the_delivered_volume_is_reported_against_the_one_target_set(tmp_path):
    """Reported, never passed: no published figure bounds the difference."""
    session = _check(_archive_with_targets(tmp_path, ["200", "0", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED
    assert session.target_volume.ratio == pytest.approx(1.0, abs=0.01)


def test_the_report_does_not_call_a_zero_volume_sample_a_targeting_off_sample(
    tmp_path,
):
    """Two exclusions, two different meanings, and the sentence names one.

    Targeting is on for all 400 samples here and 100 of them carry a delivered
    volume of zero. Counting only the samples that reached the median produced
    "300 of 400 samples where targeting was active" — false about 100 samples,
    and in the direction that understates how long the device was regulating.
    """
    session = _check(
        _archive_with_targets(tmp_path, ["200", "0", "0"], tidal_zero_samples=100)
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED
    assert "300 samples above zero among the 400 of 400" in session.target_volume.detail


def test_targeting_that_ran_with_no_volume_above_zero_says_which_it_was(tmp_path):
    """Refusing with "targeting was not active" would describe another session.

    Targeting ran for every sample and every delivered volume was zero. The
    reason a reader is given has to be the one that happened, or they go
    looking for a state channel that is behaving correctly.
    """
    session = _check(
        _archive_with_targets(tmp_path, ["200", "0", "0"], tidal_zero_samples=400)
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "no delivered volume above zero" in session.target_volume.detail


@pytest.mark.parametrize("raw", ["100", "2000"])
def test_the_settable_range_includes_both_its_endpoints(tmp_path, raw):
    """`thresholds.md` promises a test pins each threshold's boundary. None did.

    The comparison is inclusive, and nothing held it there: tightening
    ``low <= x <= high`` to ``low < x < high`` left the whole suite green,
    because every other test uses a value comfortably inside the range. A
    firmware storing exactly the manual's minimum or maximum would then have
    its check refused with a message saying the value is outside a range it is
    the edge of.
    """
    session = _check(_archive_with_targets(tmp_path, [raw, "0", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED


def test_a_stored_zero_is_not_a_target_of_zero_millilitres(tmp_path):
    """The manual's settable range starts at 100 ml, so zero is not a setting.

    Reading it as one would compare a delivered volume against nothing and
    divide by it. This is the sentinel case the format notes warn about: a raw
    zero that means "off" rather than zero.
    """
    session = _check(_archive_with_targets(tmp_path, ["0", "0", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "no enabled programme has a volume target set" in session.target_volume.detail


def test_two_enabled_programmes_with_targets_are_refused_not_resolved(tmp_path):
    """Which programme ran is not established, so the ambiguity stands.

    Picking the closer of the two would answer that question by assuming the
    answer, and the assumption would be invisible in the ratio.
    """
    session = _check(_archive_with_targets(tmp_path, ["200", "500", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "different target volumes" in session.target_volume.detail


def test_two_enabled_programmes_agreeing_on_a_target_are_not_ambiguous(tmp_path):
    """The rule is one distinct target, not one programme carrying one.

    With both enabled programmes set to the same volume, which of them ran
    makes no difference to the answer, so there is nothing to refuse.
    """
    session = _check(_archive_with_targets(tmp_path, ["200", "200", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED
    assert session.target_volume.ratio == pytest.approx(1.0, abs=0.01)


def test_a_target_that_is_not_a_number_is_refused_not_skipped(tmp_path):
    """Skipping it would let another programme's target stand unopposed.

    The old code passed over anything it could not read, so an archive with one
    unreadable target and one good one reported a clean ratio produced by
    discarding the evidence against it.
    """
    session = _check(_archive_with_targets(tmp_path, ["bad", "200", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "not a number" in session.target_volume.detail


def test_a_non_zero_target_outside_the_settable_range_is_refused(tmp_path):
    """Zero is documented as "no target"; 50 ml is not documented as anything.

    Treating an unexplained value as absent is the same mistake as skipping an
    unreadable one, and it produces the same falsely clean ratio.
    """
    session = _check(_archive_with_targets(tmp_path, ["50", "200", "0"])).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "settable" in session.target_volume.detail


def test_a_target_in_a_disabled_programme_does_not_count(tmp_path):
    """A programme that is off delivers nothing to compare against."""
    session = _check(
        _archive_with_targets(tmp_path, ["200", "0", "500"], enabled=("1", "0", "0"))
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED


def test_the_comparison_needs_targeting_to_have_been_active(tmp_path):
    """A breath delivered with targeting off has nothing to do with a target."""
    session = _check(
        _archive_with_targets(tmp_path, ["200", "0", "0"], targeting=False)
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "targeting was not active" in session.target_volume.detail


def test_a_session_without_the_state_channel_is_not_compared(tmp_path):
    """Older firmware need not write it, and its absence is not a finding."""
    session = _check(
        _archive_with_targets(tmp_path, ["200", "0", "0"], targeting=None)
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "Target Volume" in session.target_volume.detail


def test_the_target_volume_check_never_passes_or_fails(tmp_path):
    """It must not reach the exit code, however the comparison turns out."""
    for targets in (["200", "0", "0"], ["0", "0", "0"], ["200", "500", "0"]):
        session = _check(_archive_with_targets(tmp_path / str(targets), targets)).sessions[0]
        assert session.target_volume.status in {
            CheckStatus.REPORTED,
            CheckStatus.NOT_COMPARABLE,
        }


def test_the_off_state_is_read_from_the_header_not_assumed(tmp_path):
    """A state channel need not be encoded 0/1, and on this device one is not.

    The card declares `Breath Phase` as digital 1..2 and `Target Volume` as
    0..1, both mapping to physical 0..1. Testing the digital sample against
    zero works for the second and counts every sample of the first as active.
    This fixture writes the state channel in the 1/2 encoding with targeting
    off throughout: read through the header the session is not comparable,
    read as a raw digital value it would report a ratio computed over breaths
    delivered with targeting off.
    """
    import zipfile

    from synthetic import (
        REGISTERED_PARAMETER_IDS,
        SyntheticSignal,
        _parameter_map_xml,
        build_wmedf,
        session_event_xml,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    n_records = 400
    signals = [
        SyntheticSignal(
            "Tidal Volume", "ml", 0.0, 3000.0, 0, 30000, 1, 2,
            data=[2000] * n_records,
        ),
        # Off for the whole session, in the encoding the phase channel uses.
        SyntheticSignal(
            "Target Volume", "", 0.0, 1.0, 1, 2, 1, 1, data=[1] * n_records
        ),
    ]
    rows = (
        '<parameter time="+0012:00:00.000" id="6" value="1"/>'
        '<parameter time="+0012:00:00.000" id="7" value="0"/>'
        '<parameter time="+0012:00:00.000" id="8" value="0"/>'
        '<parameter time="+0012:00:00.000" id="111" value="200" program="0"/>'
        '<parameter time="+0012:00:00.000" id="69" value="1600" program="0"/>'
        '<parameter time="+0012:00:00.000" id="111" value="0" program="1"/>'
        '<parameter time="+0012:00:00.000" id="69" value="1100" program="1"/>'
    )
    log = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<log version="1" date="2020-03-04">'
        f'<config version="6.3.0"/>{rows}</log>'
    ).encode("utf-8")
    entries = (
        (6, "ProgramEnabled1"),
        (7, "ProgramEnabled2"),
        (8, "ProgramEnabled3"),
        *REGISTERED_PARAMETER_IDS,
    )
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf(signals, n_records, start=start))
        zf.writestr("event_0001.xml", session_event_xml(start, n_records))
        zf.writestr("parameter.xml", log)
        zf.writestr("parametersmap.xml", _parameter_map_xml(entries=entries))

    session = _check(path).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "targeting was not active" in session.target_volume.detail


def test_the_archive_wide_lookups_happen_once_not_once_per_session(tmp_path, monkeypatch):
    """Both answer a question about the archive, so both are asked once.

    `long_term` carries this note already, having once been forgotten by a
    caller. The target lookup parses the settings and their name map, and a
    caller that leaves it to the default re-parses both for every session
    without anything going wrong visibly — which is how the omission would
    survive.
    """
    from prisma_vent import validate as module

    calls = []
    original = module.target_volume_setting
    monkeypatch.setattr(
        module,
        "target_volume_setting",
        lambda archive: (calls.append(1), original(archive))[1],
    )
    # Two sessions, because with one "once" and "once per session" agree.
    path = _archive_with_targets(tmp_path, ["200", "0", "0"], sessions=2)
    report = _check(path)
    assert len(report.sessions) == 2
    assert calls == [1]


def _archive_with_two_snapshots(
    tmp_path, first, second, *, change_record=False, change_id=111
):
    """An archive whose settings move partway through it."""
    import zipfile

    from synthetic import (
        REGISTERED_PARAMETER_IDS,
        _parameter_map_xml,
        session_event_xml,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)

    def snapshot(offset, target):
        rows = (
            f'<parameter time="{offset}" id="6" value="1"/>'
            f'<parameter time="{offset}" id="7" value="0"/>'
            f'<parameter time="{offset}" id="8" value="0"/>'
        )
        for position in range(3):
            value = target if position == 0 else "0"
            rows += (
                f'<parameter time="{offset}" id="111" value="{value}" '
                f'program="{position}"/>'
                f'<parameter time="{offset}" id="69" value="1600" '
                f'program="{position}"/>'
            )
        return rows

    body = snapshot("+0012:00:00.000", first)
    if change_record:
        # A single entry, which is what the device writes when one setting is
        # changed while it is recording.
        body += f'<parameter time="+0024:00:00.000" id="{change_id}" value="{second}"/>'
    else:
        body += snapshot("+0024:00:00.000", second)

    log = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<log version="1" date="2020-03-04">'
        f'<config version="6.3.0"/>{body}</log>'
    ).encode("utf-8")
    entries = (
        (6, "ProgramEnabled1"),
        (7, "ProgramEnabled2"),
        (8, "ProgramEnabled3"),
        *REGISTERED_PARAMETER_IDS,
    )

    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", _breathing_session(400, targeting=True))
        zf.writestr("event_0001.xml", session_event_xml(start, 400))
        zf.writestr("parameter.xml", log)
        zf.writestr("parametersmap.xml", _parameter_map_xml(entries=entries))
    return path


def test_a_target_that_moves_within_the_archive_is_refused(tmp_path):
    """Reading the last snapshot would date the setting wrongly.

    A 200 ml night compared against a 500 ml target dialled in the following
    noon reports a ratio near 0.4 and reads as a decoding fault. Reconstructing
    which settings were in force for each session is the eventual answer; until
    then the archive is refused rather than answered from whichever snapshot
    happens to be last.
    """
    session = _check(_archive_with_two_snapshots(tmp_path, "200", "500")).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "do not agree on one target" in session.target_volume.detail


def test_a_change_record_touching_the_target_is_refused_too(tmp_path):
    """A change is one entry, not a snapshot, and would otherwise be invisible.

    Comparing full snapshots alone would miss it: the device writes a single
    parameter when one setting moves while it is recording.
    """
    session = _check(
        _archive_with_two_snapshots(tmp_path, "200", "500", change_record=True)
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "altered the volume target" in session.target_volume.detail


def test_a_map_that_never_names_the_enable_flags_says_so(tmp_path):
    """"Nothing says which are enabled" is not "none is enabled".

    The default synthetic archive is exactly this case: two therapy blocks and
    a map carrying no ``ProgramEnabled*`` names at all — nothing requires them,
    since the registry gates on the ids it publishes scales for and these are
    not among them. Reading the absent flags as "off" put a claim about the
    therapy into a report where the archive had said nothing, which is the
    failure this module's own comments call out twice elsewhere.
    """
    path = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 400)],
    )
    session = _check(path).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert "does not name which programmes are enabled" in session.target_volume.detail


def test_an_unrecognised_enable_flag_is_not_read_as_disabled(tmp_path):
    """The failure this hides is a competing target vanishing quietly.

    Blocks carry 200 ml and 500 ml, and the second block's flag reads
    ``"bad"``. Treating anything but ``"1"`` as off dropped that block, left
    200 ml standing as the archive's one unambiguous target, and reported a
    ratio of exactly 1.000 — the most convincing possible output, produced by
    discarding the evidence against it. Which programme delivered therapy is
    not established, so an unreadable flag cannot be resolved either way.
    """
    session = _check(
        _archive_with_targets(
            tmp_path, ["200", "500", "0"], enabled=("1", "bad", "0")
        )
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert session.target_volume.ratio is None
    assert "neither enabled nor disabled" in session.target_volume.detail


def test_a_missing_enable_flag_hides_no_competing_target_either(tmp_path):
    """The same archive with the flag absent rather than unreadable.

    Two ways of not knowing, and neither may resolve to "off". Kept beside the
    unreadable case because a fix for one that leaves the other silently
    reporting 1.000 would look like a fix.
    """
    session = _check(
        _archive_with_targets(tmp_path, ["200", "500", "0"], enabled=("1",))
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert session.target_volume.ratio is None
    assert "does not name which programmes are enabled" in session.target_volume.detail


def test_blocks_numbered_outside_the_flag_run_are_refused(tmp_path):
    """`ProgramEnabledN` counts from one over the blocks in order.

    A block positioned outside that run matches no flag, so it was skipped with
    nothing said — the same silent disappearance as an unreadable flag, reached
    from the other side.
    """
    session = _check(
        _archive_with_targets(
            tmp_path, ["200", "500", "0"], positions=(0, 1, 7)
        )
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.NOT_COMPARABLE
    assert session.target_volume.ratio is None
    assert "not numbered from the first without gaps" in session.target_volume.detail


def test_a_neighbouring_volume_target_parameter_does_not_refuse(tmp_path):
    """Three real parameter names begin with ``VolumeTarget``; one is the target.

    ``VolumeTargetControl`` and ``VolumeTargetDeltaPressure`` are separate
    settings, and changing either moves nothing this check reads. Matching the
    name by prefix — which the enabled-programme names do need, because the
    device numbers one per programme — would refuse this archive while stating
    that the target had changed. The refusal would be wrong and its reason
    untrue, which is worse: a reader would go looking for a setting change that
    never happened.
    """
    session = _check(
        _archive_with_two_snapshots(
            tmp_path, "200", "300", change_record=True, change_id=113
        )
    ).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED


def test_two_snapshots_that_agree_still_compare(tmp_path):
    """Refusing every archive with more than one snapshot would be too much."""
    session = _check(_archive_with_two_snapshots(tmp_path, "200", "200")).sessions[0]
    assert session.target_volume.status is CheckStatus.REPORTED
