<?php
/**
 * html2wp quick check: the PHP error reporter (a must-use plugin).
 *
 * Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
 *
 * quick-check.py copies this into the THROWAWAY test-env WordPress only
 * (wp-content/mu-plugins/h2wp-quick-check.php) — never into a theme, never
 * into an owner's site. `test-env.sh reset` restores the snapshot without it.
 *
 * That WordPress does not display PHP errors. For the one request that asks
 * (header X-H2WP-Quick: 1), collect the warnings, notices and fatal errors it
 * raises and append them as an HTML comment. Every other request, including
 * the owner's own browsing, is untouched.
 *
 * Carried over from the desktop app (runtime/quick-check-mu.php).
 */
defined( 'ABSPATH' ) || exit;

if ( isset( $_SERVER['HTTP_X_H2WP_QUICK'] ) && '1' === $_SERVER['HTTP_X_H2WP_QUICK'] ) {
	$GLOBALS['h2wp_quick_problems'] = array();
	error_reporting( E_ALL ); // phpcs:ignore WordPress.PHP.DevelopmentFunctions.prevent_path_disclosure_error_reporting
	set_error_handler( // phpcs:ignore WordPress.PHP.DevelopmentFunctions.error_log_set_error_handler
		function ( $number, $message, $file, $line ) {
			// An @-suppressed call is not a problem of the page.
			if ( ! ( error_reporting() & $number ) ) { // phpcs:ignore WordPress.PHP.DevelopmentFunctions.prevent_path_disclosure_error_reporting
				return true;
			}
			if ( count( $GLOBALS['h2wp_quick_problems'] ) < 20 ) {
				$GLOBALS['h2wp_quick_problems'][] = array( 'type' => $number, 'message' => substr( (string) $message, 0, 300 ), 'file' => (string) $file, 'line' => (int) $line );
			}
			return true;
		}
	);
	register_shutdown_function(
		function () {
			$last = error_get_last();
			if ( $last && in_array( $last['type'], array( E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR ), true ) ) {
				$GLOBALS['h2wp_quick_problems'][] = array( 'type' => $last['type'], 'message' => substr( (string) $last['message'], 0, 300 ), 'file' => (string) $last['file'], 'line' => (int) $last['line'] );
			}
			echo "\n<!--h2wp-quick:" . base64_encode( (string) json_encode( array( 'themeRoot' => function_exists( 'get_theme_root' ) ? get_theme_root() : '', 'problems' => $GLOBALS['h2wp_quick_problems'] ) ) ) . '-->'; // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode, WordPress.WP.AlternativeFunctions.json_encode_json_encode, WordPress.Security.EscapeOutput.OutputNotEscaped
		}
	);
}
