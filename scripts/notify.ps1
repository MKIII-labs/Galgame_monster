param(
    [Parameter(Mandatory = $true)][string]$Payload
)
# NOTE: keep this file ASCII-only. PowerShell 5.1 reads script files using the
# system ANSI codepage unless they carry a BOM, so non-ASCII here would garble.
# All localized text (Chinese title/body) is read from the UTF-8 JSON payload.
$ErrorActionPreference = 'Stop'

$raw = Get-Content -LiteralPath $Payload -Raw -Encoding UTF8
$data = $raw | ConvertFrom-Json
$title = [string]$data.title
$text = [string]$data.text
if (-not $title) { $title = 'Galgame_monster' }
if (-not $text) { $text = 'Reminder' }

# XML-escape so &, <, > in the text cannot break the toast markup
$title = [System.Security.SecurityElement]::Escape($title)
$text = [System.Security.SecurityElement]::Escape($text)

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

$tpl = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>$title</text>
      <text>$text</text>
    </binding>
  </visual>
</toast>
"@

$doc = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]::New()
$doc.LoadXml($tpl)
$toast = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]::New($doc)

# Built-in PowerShell AUMID: Windows accepts this as a notification source.
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]::CreateToastNotifier($appId).Show($toast)
