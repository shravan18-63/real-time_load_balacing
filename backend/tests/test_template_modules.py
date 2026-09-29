import importlib
import unittest


class TemplateModuleTests(unittest.TestCase):
    def test_template_model_exports_index_builder(self):
        module = importlib.import_module("app.models.template_model")

        self.assertTrue(callable(module.create_template_indexes))

    def test_template_routes_exports_blueprint(self):
        module = importlib.import_module("app.routes.template_routes")

        self.assertEqual(module.template_bp.url_prefix, "/api/templates")


if __name__ == "__main__":
    unittest.main()
