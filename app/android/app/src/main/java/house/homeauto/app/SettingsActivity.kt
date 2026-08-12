package house.homeauto.app

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import house.homeauto.app.databinding.ActivitySettingsBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

/**
 * Server endpoint + alert toggle.
 *
 * Reached by long-pressing the launcher icon ("Server settings") or from the unreachable screen. The
 * dashboard is a full-screen surface and this is touched about once per install, so it does not get to
 * charge the main screen an app-bar's worth of height for the privilege.
 *
 * "Test" probes before saving so a typo surfaces here rather than as a blank screen on the next launch.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }

        binding.endpoint.setText(prefs.endpoint)
        binding.alertsSwitch.isChecked = prefs.alertsEnabled

        binding.usePrimary.setOnClickListener { binding.endpoint.setText(Endpoints.DEFAULT_PRIMARY) }
        binding.useFallback.setOnClickListener { binding.endpoint.setText(Endpoints.DEFAULT_FALLBACK) }
        binding.test.setOnClickListener { test() }
        binding.save.setOnClickListener { save() }
    }

    private fun currentNormalized(): String? = Endpoints.normalize(binding.endpoint.text?.toString())

    private fun test() {
        val base = currentNormalized()
        if (base == null) {
            status(getString(R.string.settings_bad_url))
            return
        }
        status(getString(R.string.settings_testing))
        binding.test.isEnabled = false
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { probe(base) }
            binding.test.isEnabled = true
            status(
                when (result) {
                    ProbeResult.OK -> getString(R.string.settings_reachable, base)
                    ProbeResult.CERT -> getString(R.string.settings_cert_rejected, base)
                    ProbeResult.UNREACHABLE -> getString(R.string.settings_unreachable, base)
                }
            )
        }
    }

    private fun save() {
        val base = currentNormalized()
        if (base == null) {
            status(getString(R.string.settings_bad_url))
            return
        }
        prefs.endpoint = base
        prefs.alertsEnabled = binding.alertsSwitch.isChecked
        // The alert lane is bound to the endpoint, so a change has to restart it — otherwise it keeps
        // talking to the old broker until the next reboot and the setting silently means nothing.
        if (prefs.alertsEnabled) AlertService.restart(this) else AlertService.stop(this)
        finish()
    }

    private fun status(text: String) {
        binding.status.visibility = View.VISIBLE
        binding.status.text = text
    }

    private enum class ProbeResult { OK, CERT, UNREACHABLE }

    /**
     * Distinguishes "nothing there" from "something there presenting the wrong certificate" — the second
     * is the far more confusing failure, and worth naming explicitly since the app pins.
     */
    private fun probe(base: String): ProbeResult = try {
        val c = URL(Endpoints.healthUrl(base)).openConnection() as HttpURLConnection
        c.connectTimeout = 4000
        c.readTimeout = 4000
        val ok = c.responseCode in 200..299
        c.disconnect()
        if (ok) ProbeResult.OK else ProbeResult.UNREACHABLE
    } catch (e: javax.net.ssl.SSLHandshakeException) {
        ProbeResult.CERT
    } catch (_: Exception) {
        ProbeResult.UNREACHABLE
    }
}
