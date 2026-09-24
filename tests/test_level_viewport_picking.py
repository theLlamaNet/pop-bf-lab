"""Geometry checks for selecting the nearest visible scene mesh."""
import unittest

from bf_lab.level_viewport import ray_triangle


class LevelViewportPickingTests(unittest.TestCase):
    def test_ray_hits_triangle_from_either_side(self):
        triangle = ((-1, -1, 0), (1, -1, 0), (0, 1, 0))
        self.assertAlmostEqual(ray_triangle((0, 0, 2), (0, 0, -1), *triangle), 2)
        self.assertAlmostEqual(ray_triangle((0, 0, -2), (0, 0, 1), *triangle), 2)

    def test_ray_misses_outside_or_behind_triangle(self):
        triangle = ((-1, -1, 0), (1, -1, 0), (0, 1, 0))
        self.assertIsNone(ray_triangle((2, 0, 2), (0, 0, -1), *triangle))
        self.assertIsNone(ray_triangle((0, 0, 2), (0, 0, 1), *triangle))
