-- scope: run
-- Integration step I2 (ADR 0029): which of a step's LIFECYCLE..VERIFY stages completed. After a
-- crash, the restart finishes the interrupted step from the stage that did not complete, so a
-- stage never runs again after a later stage acted (DETECT must not see GOVERN's changes of the
-- same minute, or the crashed run would diverge from a clean one).
CREATE TABLE loop_stage (
    step_no int  NOT NULL,
    stage   text NOT NULL CHECK (stage IN ('lifecycle', 'detect', 'decide', 'govern', 'verify')),
    PRIMARY KEY (step_no, stage)
);
