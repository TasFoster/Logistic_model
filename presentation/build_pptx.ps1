# Builds the .pptx via PowerPoint COM. ASCII-only code; all Cyrillic text comes from
# slides.json read as UTF-8 (avoids PS 5.1 script-encoding corruption).
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataPath = Join-Path $here "slides.json"
$json = [System.IO.File]::ReadAllText($dataPath, [System.Text.Encoding]::UTF8)
$data = $json | ConvertFrom-Json

function Rgb([int]$r, [int]$g, [int]$b) { return ($r + $g * 256 + $b * 65536) }
$BG    = Rgb 14 26 43
$PANEL = Rgb 22 38 60
$ACC   = Rgb 45 212 191
$ACC2  = Rgb 245 158 11
$TXT   = Rgb 226 232 240
$MUT   = Rgb 148 163 184
$WHT   = Rgb 255 255 255
$W = 960; $H = 540
$FONT = "Segoe UI"
$CR = [char]13
$LF = [char]10
$BULLET = [char]0x2022

function New-Text($slide, $l, $t, $w, $h, $text, $size, $color, $bold, $align, $anchorMid) {
  $tb = $slide.Shapes.AddTextbox(1, $l, $t, $w, $h)
  $tb.TextFrame.WordWrap = -1
  $tb.TextFrame.AutoSize = 0
  if ($anchorMid) { $tb.TextFrame.VerticalAnchor = 3 }
  $tr = $tb.TextFrame.TextRange
  $tr.Text = $text
  $tr.Font.Size = $size
  $tr.Font.Name = $FONT
  $tr.Font.Color.RGB = $color
  $tr.Font.Bold = $bold
  $tr.ParagraphFormat.Alignment = $align
  $tr.ParagraphFormat.SpaceAfter = 8
  return $tb
}

function New-Box($slide, $l, $t, $w, $h, $text, $fill, $border) {
  $sh = $slide.Shapes.AddShape(5, $l, $t, $w, $h)   # 5 = rounded rectangle
  $sh.Fill.ForeColor.RGB = $fill
  $sh.Line.ForeColor.RGB = $border
  $sh.Line.Weight = 1.5
  $sh.TextFrame.VerticalAnchor = 3
  $tr = $sh.TextFrame.TextRange
  $tr.Text = $text.Replace($LF, $CR)
  $tr.Font.Size = 14
  $tr.Font.Name = $FONT
  $tr.Font.Color.RGB = $WHT
  $tr.Font.Bold = 1
  $tr.ParagraphFormat.Alignment = 2
  return $sh
}

function New-Arrow($slide, $x1, $y1, $x2, $y2) {
  $ln = $slide.Shapes.AddLine($x1, $y1, $x2, $y2)
  $ln.Line.ForeColor.RGB = $MUT
  $ln.Line.Weight = 1.75
  $ln.Line.EndArrowheadStyle = 2
  $ln.Line.EndArrowheadLength = 2
  $ln.Line.EndArrowheadWidth = 2
  return $ln
}

$ppt = New-Object -ComObject PowerPoint.Application
$ppt.Visible = 1
$ppt.DisplayAlerts = 1
try {
  $pres = $ppt.Presentations.Add()
  $pres.PageSetup.SlideWidth = $W
  $pres.PageSetup.SlideHeight = $H
  $total = $data.slides.Count
  $idx = 0

  foreach ($s in $data.slides) {
    $idx++
    $slide = $pres.Slides.Add($idx, 12)   # 12 = blank layout

    # background (var name must NOT collide with $BG — PowerShell is case-insensitive)
    $bgRect = $slide.Shapes.AddShape(1, 0, 0, $W, $H)
    $bgRect.Fill.ForeColor.RGB = $BG
    $bgRect.Line.Visible = 0

    if ($s.type -eq "title") {
      New-Text $slide 60 165 840 120 $s.title 40 $WHT 1 2 $false | Out-Null
      $ul = $slide.Shapes.AddShape(1, 430, 300, 100, 4); $ul.Fill.ForeColor.RGB = $ACC; $ul.Line.Visible = 0
      New-Text $slide 80 312 800 46 $s.subtitle 19 $ACC 0 2 $false | Out-Null
      New-Text $slide 80 384 800 100 (($s.lines) -join $CR) 13 $MUT 0 2 $false | Out-Null
      continue
    }

    # non-title chrome: left accent bar, title, underline, footer, page number
    $bar = $slide.Shapes.AddShape(1, 0, 0, 10, $H); $bar.Fill.ForeColor.RGB = $ACC; $bar.Line.Visible = 0
    New-Text $slide 46 30 868 52 $s.title 27 $WHT 1 1 $false | Out-Null
    $tul = $slide.Shapes.AddShape(1, 50, 88, 110, 4); $tul.Fill.ForeColor.RGB = $ACC; $tul.Line.Visible = 0
    New-Text $slide 40 514 760 20 $data.footer 9 $MUT 0 1 $false | Out-Null
    New-Text $slide 820 514 120 20 ("$idx / $total") 9 $MUT 0 3 $false | Out-Null

    switch ($s.type) {
      "bullets" {
        $maxlen = ($s.bullets | Measure-Object -Property Length -Maximum).Maximum
        $size = if ($maxlen -gt 95) { 15 } else { 16 }
        $lines = @()
        foreach ($b in $s.bullets) { $lines += ("$BULLET  " + $b) }
        New-Text $slide 50 112 862 388 ($lines -join $CR) $size $TXT 0 1 $false | Out-Null
      }
      "two-col" {
        $ll = @(); foreach ($b in $s.left)  { $ll += ("$BULLET  " + $b) }
        $rr = @(); foreach ($b in $s.right) { $rr += ("$BULLET  " + $b) }
        New-Text $slide 50  120 420 380 ($ll -join $CR) 16 $TXT 0 1 $false | Out-Null
        New-Text $slide 500 120 414 380 ($rr -join $CR) 16 $TXT 0 1 $false | Out-Null
      }
      "diagram" {
        New-Box $slide 280 108 400 62 $s.boxes.graph $PANEL $ACC  | Out-Null
        New-Box $slide 110 246 330 74 $s.boxes.ana   $PANEL $ACC  | Out-Null
        New-Box $slide 520 246 330 74 $s.boxes.sim   $PANEL $ACC2 | Out-Null
        New-Box $slide 280 388 400 58 $s.boxes.check $PANEL $ACC  | Out-Null
        New-Arrow $slide 430 170 300 246 | Out-Null
        New-Arrow $slide 540 170 670 246 | Out-Null
        New-Arrow $slide 300 320 440 388 | Out-Null
        New-Arrow $slide 670 320 540 388 | Out-Null
        New-Text $slide 60 462 840 40 $s.caption 14 $ACC2 0 2 $false | Out-Null
      }
      "table" {
        $cols = $s.headers.Count
        $rows = $s.rows.Count + 1
        $th = [Math]::Min(300, 42 * $rows)
        $shp = $slide.Shapes.AddTable($rows, $cols, 50, 118, 862, $th)
        $tbl = $shp.Table
        try { $tbl.FirstRow = $false } catch {}
        try { $tbl.HorizBanding = $false } catch {}
        # column widths: first column wider
        $first = if ($cols -eq 5) { 210 } else { 250 }
        $rest = [int](($862 - $first) / ($cols - 1))
        $tbl.Columns.Item(1).Width = $first
        for ($c = 2; $c -le $cols; $c++) { $tbl.Columns.Item($c).Width = $rest }
        # header row
        for ($c = 1; $c -le $cols; $c++) {
          $cell = $tbl.Cell(1, $c)
          $cell.Shape.Fill.ForeColor.RGB = $ACC
          $cf = $cell.Shape.TextFrame; $cf.VerticalAnchor = 3
          $ctr = $cf.TextRange; $ctr.Text = [string]$s.headers[$c - 1]
          $ctr.Font.Size = 13; $ctr.Font.Bold = 1; $ctr.Font.Name = $FONT; $ctr.Font.Color.RGB = $BG
        }
        # body rows
        for ($r = 0; $r -lt $s.rows.Count; $r++) {
          $rowFill = if ($r % 2 -eq 0) { $BG } else { $PANEL }
          for ($c = 1; $c -le $cols; $c++) {
            $cell = $tbl.Cell($r + 2, $c)
            $cell.Shape.Fill.ForeColor.RGB = $rowFill
            $cf = $cell.Shape.TextFrame; $cf.VerticalAnchor = 3
            $ctr = $cf.TextRange; $ctr.Text = [string]$s.rows[$r][$c - 1]
            $ctr.Font.Size = 12; $ctr.Font.Name = $FONT
            $ctr.Font.Color.RGB = if ($c -eq 1) { $WHT } else { $TXT }
            $ctr.Font.Bold = if ($c -eq 1) { 1 } else { 0 }
          }
        }
        if ($s.note) { New-Text $slide 50 (118 + $th + 16) 862 60 $s.note 13 $ACC2 0 1 $false | Out-Null }
      }
    }
  }

  # output path: absolute as-is, otherwise resolved against the repo root (parent of this script's dir)
  $out = if ([System.IO.Path]::IsPathRooted($data.output)) { $data.output } else { Join-Path (Split-Path -Parent $here) $data.output }
  if (Test-Path $out) { Remove-Item $out -Force }
  $pres.SaveAs($out, 24)    # 24 = ppSaveAsOpenXMLPresentation (.pptx)
  $pres.Close()
  "SAVED: $out  (slides: $total)"
}
finally {
  $ppt.Quit()
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($ppt) | Out-Null
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
