import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
INDEX_TEMPLATE = (ROOT / "index.html").read_text(encoding="utf-8")
MAIN_TREE = ast.parse(MAIN_SOURCE)


def string_constant(name):
    for node in MAIN_TREE.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"Constant {name} was not found")


def rendered_index(title, description, canonical):
    return (
        INDEX_TEMPLATE
        .replace("__PAGE_TITLE__", title)
        .replace("__PAGE_DESCRIPTION__", description)
        .replace("__PAGE_CANONICAL__", canonical)
    )


class PageMetadataTests(unittest.TestCase):
    def test_tryon_has_server_rendered_metadata(self):
        title = string_constant("TRYON_PAGE_TITLE")
        description = string_constant("TRYON_PAGE_DESCRIPTION")
        html = rendered_index(title, description, "https://styleglobe.ru/try-on")

        self.assertIn(f"<title>{title}</title>", html)
        self.assertIn(f'<meta name="description" content="{description}">', html)
        self.assertIn(f'<meta property="og:title" content="{title}">', html)
        self.assertIn(
            '<link rel="canonical" href="https://styleglobe.ru/try-on">',
            html,
        )
        self.assertNotIn("__PAGE_", html)

    def test_tryon_route_passes_its_metadata_to_page_renderer(self):
        route = next(
            node for node in MAIN_TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "p_tryon"
        )
        call = next(
            node for node in ast.walk(route)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_page"
        )
        keywords = {item.arg: item.value for item in call.keywords}

        self.assertEqual(ast.unparse(keywords["title"]), "TRYON_PAGE_TITLE")
        self.assertEqual(ast.unparse(keywords["description"]), "TRYON_PAGE_DESCRIPTION")
        self.assertEqual(ast.unparse(keywords["canonical"]), "f'{SITE_URL}/try-on'")

    def test_seller_keeps_seller_metadata(self):
        title = string_constant("SELLER_PAGE_TITLE")
        description = string_constant("SELLER_PAGE_DESCRIPTION")
        html = rendered_index(title, description, "https://styleglobe.ru/seller")

        self.assertIn(
            "<title>StyleGlobe — каталожные фото для Wildberries и Ozon</title>",
            html,
        )
        self.assertIn(
            '<link rel="canonical" href="https://styleglobe.ru/seller">',
            html,
        )


if __name__ == "__main__":
    unittest.main()
