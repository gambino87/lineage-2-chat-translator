package l2ctsecurity

import "os"

func TextOnlyProjectors(projectors []string) []string {
	if os.Getenv("L2CT_TEXT_ONLY") == "1" {
		return nil
	}
	return projectors
}
