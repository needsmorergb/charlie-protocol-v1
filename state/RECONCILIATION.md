mint                   8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump
observed at            2026-08-31 21:54:49Z  (epoch 1788213289)
burn cursor            4dCeh2PBo3xThHExX87wMRgcyCs44tJUdKLgjEcJC5cvRxM9Rpjfr7XtmRwPu67uZeR3nDzNEmpREQUZSCivmHfT
burn walk              complete

THE FIGURE TO EXPLAIN
    initial_supply         1,000,000,000,000,000  raw units  (1,000,000,000.000000)
    live_supply            956,383,374,035,955  raw units  (956,383,374.035955)
    implied_total_burned   43,616,625,964,045  raw units  (43,616,625.964045)  (initial_supply - live_supply)
    attributed_burned      43,576,580,427,900  raw units  (43,576,580.427900)  (every burn this scan has recorded so far)
        boost              43,575,480,427,900  raw units  (43,575,480.427900)  (pump's boost authority, at migration -- supply destroyed, not protocol-attributed, D-11)
        non-boost               1,100,000,000  raw units  (1,100.000000)  (every other recorded burn -- a stranger burning their own tokens still counts, D-09)
    residual                   40,045,536,145  raw units  (40,045.536145)  (implied_total_burned - attributed_burned)

This residual is correct AS OF the observation above -- not a fixed historical
gap. $CHARLIE's supply is still falling: a coin whose holders keep burning
tokens directly, well after any one-shot event, will show a different residual
at the next observation. That is the expected behaviour of an actively-burning
coin, not evidence of an error, and no future observation supersedes this one --
both remain readable as what was true at their own moment.

If you have seen a smaller figure for this same gap quoted elsewhere, it is the
same quantity measured from a different, earlier baseline -- subtracting that
baseline's own estimate of already-known non-boost burns from the residual above
reproduces it. The two are not in conflict; they are two measurements of the same
thing from two different starting points.
