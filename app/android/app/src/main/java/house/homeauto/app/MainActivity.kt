package house.homeauto.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import house.homeauto.app.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

/**
 * The shell. Hosts the PWA in a WebView and owns exactly the things a browser could not do for us:
 * a pinned-trust origin, an app identity, and a place to launch the alert service from.
 *
 * The UI is NOT bundled — it is fetched from the server every launch. That is the whole design (ADR-0038):
 * a PWA change deploys to ha-2 and every client, phone included, has it without a new APK.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs
    private var loadedOk = false

    private val requestNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* mute if denied */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // targetSdk 35 is edge-to-edge by default; without this the dashboard renders under the status bar.
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }

        Notifications.ensureChannels(this)
        configureWebView()

        binding.retry.setOnClickListener { load() }
        binding.settings.setOnClickListener { openSettings() }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.web.canGoBack()) binding.web.goBack() else finish()
            }
        })

        askForNotificationsIfNeeded()
        load()
    }

    override fun onResume() {
        super.onResume()
        // Coming back from Settings (or from a spell out of the house) — re-probe and reload if we are
        // currently sitting on the error screen or pointed at a stale origin.
        if (!loadedOk) load()
        maybeStartAlertService()
    }

    private fun configureWebView() = with(binding.web.settings) {
        javaScriptEnabled = true
        domStorageEnabled = true          // the PWA keeps its derived admin token here
        // No local file or content:// access: this WebView has exactly one job, loading our origin.
        allowFileAccess = false
        allowContentAccess = false
        cacheMode = WebSettings.LOAD_DEFAULT
        mediaPlaybackRequiresUserGesture = true

        binding.web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(v: WebView?, req: WebResourceRequest?): Boolean {
                val url = req?.url?.toString() ?: return false
                // Anything off our host is a real external link — hand it to the browser rather than
                // trapping the user in a chrome-less WebView with no address bar.
                if (Endpoints.isSameHost(prefs.endpoint, url)) return false
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                return true
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                if (url != null && !url.startsWith("about:")) {
                    loadedOk = true
                    showError(null)
                }
            }

            override fun onReceivedError(v: WebView?, req: WebResourceRequest?, err: WebResourceError?) {
                // Sub-resource failures are not worth a full-screen error; only the main document is.
                if (req?.isForMainFrame != true) return
                loadedOk = false
                showError(getString(R.string.err_unreachable, prefs.endpoint))
            }

            override fun onReceivedSslError(v: WebView?, handler: SslErrorHandler?, err: SslError?) {
                // NEVER proceed(). The pin in network_security_config.xml is the app's security model;
                // clicking through it here would silently reduce us to the browser behaviour we built
                // this app to escape. Fail closed and say so.
                handler?.cancel()
                loadedOk = false
                showError(getString(R.string.err_cert, prefs.endpoint))
            }
        }
    }

    private fun load() {
        val base = prefs.endpoint
        showError(null)
        binding.progress.visibility = View.VISIBLE
        lifecycleScope.launch {
            val reachable = withContext(Dispatchers.IO) { probe(base) }
            binding.progress.visibility = View.GONE
            if (reachable) {
                binding.web.loadUrl(Endpoints.appUrl(base))
            } else {
                loadedOk = false
                showError(getString(R.string.err_unreachable, base))
            }
            maybeStartAlertService()
        }
    }

    /** Cheap liveness check against /health so we can show a real message instead of a WebView error page. */
    private fun probe(base: String): Boolean = try {
        val c = URL(Endpoints.healthUrl(base)).openConnection() as HttpURLConnection
        c.connectTimeout = 4000
        c.readTimeout = 4000
        c.requestMethod = "GET"
        val ok = c.responseCode in 200..299
        c.disconnect()
        ok
    } catch (_: Exception) {
        false
    }

    private fun showError(message: String?) {
        binding.errorPanel.visibility = if (message == null) View.GONE else View.VISIBLE
        binding.web.visibility = if (message == null) View.VISIBLE else View.GONE
        binding.errorText.text = message ?: ""
    }

    private fun openSettings() = startActivity(Intent(this, SettingsActivity::class.java))

    private fun askForNotificationsIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun maybeStartAlertService() {
        if (prefs.alertsEnabled) AlertService.start(this)
    }

    override fun onDestroy() {
        binding.web.destroy()
        super.onDestroy()
    }
}
