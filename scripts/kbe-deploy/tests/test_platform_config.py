from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kbe_deploy as cli


class Rk3568AppComponentConfigTests(unittest.TestCase):
    def test_rk3568_policy_service_is_server_64bit_only(self):
        artifacts = cli.config()['platforms']['rk3568']['modules']['audio-framework']['artifacts']
        paths = {a['path'] for a in artifacts}
        self.assertNotIn('system/lib/libaudiopolicyservice.so', paths)
        self.assertIn('system/lib64/libaudiopolicyservice.so', paths)
        self.assertIn('system/lib/libaudioclient.so', paths)

    def test_settings_and_device_control_are_complete_system_ext_apks(self):
        platform = cli.config()["platforms"]["rk3568"]
        selected = {module["id"]: module for module in cli.select_modules(
            platform, "settings,device-control")}

        self.assertEqual(selected["settings"]["targets"], ["KBESettings"])
        self.assertEqual(selected["settings"]["package"], "com.kaiboer.kbesettings")
        self.assertEqual(selected["settings"]["apk_path"],
                         "system_ext/app/KBESettings/KBESettings.apk")
        self.assertEqual(selected["settings"]["artifacts"], [{
            "path": "system_ext/app/KBESettings/KBESettings.apk", "kind": "apk"
        }])

        self.assertEqual(selected["device-control"]["targets"],
                         ["KBEDeviceControlService"])
        self.assertEqual(selected["device-control"]["package"],
                         "com.kaiboer.devicecontrolservice")
        self.assertEqual(selected["device-control"]["apk_path"],
                         "system_ext/priv-app/KBEDeviceControlService/KBEDeviceControlService.apk")
        self.assertEqual(selected["device-control"]["artifacts"], [{
            "path": "system_ext/priv-app/KBEDeviceControlService/KBEDeviceControlService.apk",
            "kind": "apk"
        }])


class Rk3576BuildConfigTests(unittest.TestCase):
    def test_services_builds_jar_and_explicit_art_runtime_targets(self):
        platform = cli.config()["platforms"]["rk3576"]
        module = cli.select_modules(platform, "services")[0]
        self.assertEqual(module["targets"], [
            "services",
            "out/target/product/rk3576_u/system/framework/oat/arm64/services.art",
            "out/target/product/rk3576_u/system/framework/oat/arm64/services.odex",
            "out/target/product/rk3576_u/system/framework/oat/arm64/services.vdex",
        ])
        self.assertEqual({item["path"] for item in module["artifacts"]}, {
            "system/framework/services.jar",
            "system/framework/oat/arm64/services.art",
            "system/framework/oat/arm64/services.odex",
            "system/framework/oat/arm64/services.vdex",
        })

    def test_engine_test_client_is_debug_apk_component(self):
        module = cli.config()["platforms"]["rk3576"]["modules"]["engine-test-client"]
        self.assertEqual(module["targets"], ["KbeAudioEngineTestClient"])
        self.assertEqual(module["package"], "com.kaiboer.audioengine.test")
        self.assertEqual(module["artifacts"][0]["path"],
                         "system_ext/priv-app/KbeAudioEngineTestClient/KbeAudioEngineTestClient.apk")


if __name__ == "__main__":
    unittest.main()
