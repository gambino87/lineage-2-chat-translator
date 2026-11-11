package l2ctsecurity

import "testing"

func TestTextOnlyProjectors(t *testing.T) {
	for _, mode := range []string{"", "0", "1"} {
		t.Run("mode_"+mode, func(t *testing.T) {
			t.Setenv("L2CT_TEXT_ONLY", mode)
			projectors := []string{"separate-projector.gguf", "inline-model.gguf"}
			actual := TextOnlyProjectors(projectors)
			if mode == "1" {
				if len(actual) != 0 {
					t.Fatal("text-only mode retained projectors")
				}
			} else if len(actual) != 2 || actual[0] != projectors[0] || actual[1] != projectors[1] {
				t.Fatal("normal mode changed projectors")
			}
			if len(projectors) != 2 {
				t.Fatal("source projector list was mutated")
			}
			if len(TextOnlyProjectors(nil)) != 0 {
				t.Fatal("text model acquired a projector")
			}
		})
	}
}
