// Runtime settings, loaded before the app (index.html). Edit this one file in the S3 bucket when the
// EC2 public IP changes (it changes on every stop/start); no rebuild needed. Upload it with
// Cache-Control: no-cache so phones pick up the change.
window.WARDLINE_CONFIG = {
  apiUrl: 'http://32.192.240.107:8000',
}
