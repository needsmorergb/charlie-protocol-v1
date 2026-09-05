<#
.SYNOPSIS
    Run the Charlie Protocol BURN leg from your own wallet, on a schedule.

.DESCRIPTION
    A thin launcher around `python -m indexer buyback`. Without -Send it
    quotes one lot against the live pool, builds the buy-and-burn
    transaction, simulates it against mainnet and prints the plan -- nothing
    is signed. With -Send it signs with the keypair file, sends, waits for
    confirmation, then reads the transaction back through the indexer's own
    decoders and prints what the coin's page will record.

    With -Send and -EverySeconds it becomes the keeper: one fixed lot per
    interval until -MaxTotalSol has been committed, every crank logged as a
    JSON line to -LogFile.

    The keypair never leaves the file. Keep the file where only you can read
    it, and fund that wallet with only what you mean to spend.

.EXAMPLE
    # quote and simulate one 0.05 SOL crank, sign nothing
    .\scripts\Start-CharlieBuyback.ps1 -Keypair $env:USERPROFILE\.config\solana\id.json

.EXAMPLE
    # one crank, sent
    .\scripts\Start-CharlieBuyback.ps1 -Keypair .\keeper.json -Send

.EXAMPLE
    # keeper: 0.05 SOL every hour, stop at 2 SOL, burn 100000 held tokens alongside each buy
    .\scripts\Start-CharlieBuyback.ps1 -Keypair .\keeper.json -Send -EverySeconds 3600 -MaxTotalSol 2 -AlsoBurn 100000
#>
[CmdletBinding()]
param(
    [string]$Mint = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump",
    [Parameter(Mandatory = $true)][string]$Keypair,
    [double]$LotSol = 0.05,
    [int]$SlippageBps = 100,
    [double]$AlsoBurn = 0,
    [int]$PriorityFee = 0,
    [int]$EverySeconds = 0,
    [double]$MaxTotalSol = 0,
    [int]$MaxCranks = 0,
    [string]$Rpc = $env:CHARLIE_RPC_URLS,
    [switch]$Send,
    [switch]$Json,
    [string]$LogFile = "buyback.log"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path $Keypair)) {
    throw "keypair file not found: $Keypair"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw "python 3.11+ is required and was not found on PATH" }

$cliArgs = @(
    "-m", "indexer", "buyback", $Mint,
    "--keypair", $Keypair,
    "--lot", $LotSol,
    "--slippage-bps", $SlippageBps,
    "--also-burn", $AlsoBurn,
    "--priority-fee", $PriorityFee
)
if ($Rpc)             { $cliArgs += @("--rpc", $Rpc) }
if ($Send)            { $cliArgs += "--send" }
if ($Json)            { $cliArgs += "--json" }
if ($EverySeconds -gt 0) {
    if (-not $Send) { throw "-EverySeconds runs the keeper and needs -Send" }
    $cliArgs += @("--every", $EverySeconds)
    if ($MaxTotalSol -gt 0) { $cliArgs += @("--max-total", $MaxTotalSol) }
    if ($MaxCranks -gt 0)   { $cliArgs += @("--max-cranks", $MaxCranks) }
}

Push-Location $repo
try {
    & $python.Source @cliArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $code
