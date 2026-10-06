"""PS-22: stack a target from the PhotonScript library on the desktop.

One command (``photonscript integrate``) turns the hand-run M31_OSC4 v4b work
(PS-109) into a repeatable pipeline:

    select   (select.py)    approved lights for a target + rig from the
                            desktop Library mirror (read-only)
    star QA  (star_qa.py)   per-sub star checks incl. the second-star-set
                            (ghost) check from qa_v4b_*.py
    calib    (calib.py)     bias / darks / flats matched on instrument, gain,
                            offset, temperature, readout (PS-128); scaled
                            darks when no dark of the light's length exists
    stage    (pipeline.py)  COPY into a new staging run folder + manifest
    PJSR     (pjsr.py)      generate integrate_stack.js / finish_stack.js
                            from deploy/ templates, checked for the PJSR rules
    run      (runner.py)    one PixInsight at a time, logs polled with short
                            reads (never held open)
    packet   (astrobin.py)  AstroBin acquisition CSV + packet MD draft

Every stage is timed into <run>/out/timing.csv.
"""
